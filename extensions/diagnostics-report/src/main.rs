//! Euler diagnostics-report extension over the managed-process protocol.
//!
//! A faithful port of the bundled in-process extension: it reads the current
//! session diagnostics log through the host and emits an aggregate report as
//! an artifact, nothing else. Input, artifact, and result shapes are unchanged
//! (`euler.diagnostics.report.v1`).

use euler_managed_process_sdk::{serve, ArtifactWrite, CommandContext, Error, Handler, Host};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

const DISPLAY_NAME: &str = "Diagnostics Report";
const COMMAND_NAME: &str = "report";
const SCHEMA: &str = "euler.diagnostics.report.v1";
const MEDIA_TYPE: &str = "application/vnd.euler.diagnostics-report.v1+json";
const DEFAULT_TAIL_LINES: u64 = 2048;
const REPORT_MAX_BYTES: u64 = 1024 * 1024;

fn main() {
    let mut handlers: BTreeMap<String, Handler<std::io::StdinLock<'static>, std::io::Stdout>> =
        BTreeMap::new();
    handlers.insert(COMMAND_NAME.to_owned(), Box::new(execute));
    serve(handlers);
}

fn execute<R: std::io::BufRead, W: std::io::Write>(
    context: &CommandContext,
    host: &mut Host<'_, R, W>,
) -> Result<Value, Error> {
    let input = ReportInput::parse(&context.input)?;
    let page = host.read_diagnostics(input.tail_lines, REPORT_MAX_BYTES)?;
    let lines: Vec<String> = page
        .get("lines")
        .and_then(Value::as_array)
        .map(|array| {
            array
                .iter()
                .filter_map(|value| value.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    let truncated = page
        .get("truncated")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if lines.is_empty() {
        return Err(input_error("no diagnostics available for this session"));
    }
    let report = Report::from_lines(&lines, truncated).to_json();
    let bytes = serde_json::to_vec(&report).map_err(|error| Error::Command(error.to_string()))?;
    let record = host.write_artifact(&ArtifactWrite {
        display_name: DISPLAY_NAME.to_owned(),
        media_type: MEDIA_TYPE.to_owned(),
        bytes,
        source_event_ids: Vec::new(),
        metadata: metadata(&report),
    })?;
    Ok(output(&record, &report))
}

#[derive(Debug, Eq, PartialEq)]
struct ReportInput {
    tail_lines: u64,
}

impl ReportInput {
    fn parse(value: &Value) -> Result<Self, Error> {
        if value.is_null() {
            return Ok(Self::default());
        }
        let object = value
            .as_object()
            .ok_or_else(|| input_error("diagnostics-report input must be a JSON object"))?;
        reject_unknown_fields(object)?;
        Ok(Self {
            tail_lines: parse_tail_lines(object)?,
        })
    }
}

impl Default for ReportInput {
    fn default() -> Self {
        Self {
            tail_lines: DEFAULT_TAIL_LINES,
        }
    }
}

#[derive(Debug, Default, Eq, PartialEq)]
struct Report {
    lines_scanned: usize,
    malformed_lines: usize,
    truncated: bool,
    event_counts: BTreeMap<String, usize>,
    duration_ms: BTreeMap<String, DurationStats>,
    ok_false_counts: BTreeMap<String, usize>,
    permission_allowed: usize,
    permission_denied: usize,
}

impl Report {
    fn from_lines(lines: &[String], truncated: bool) -> Self {
        let mut report = Self {
            lines_scanned: lines.len(),
            truncated,
            ..Self::default()
        };
        let mut durations: BTreeMap<String, Vec<u64>> = BTreeMap::new();
        for line in lines {
            let Some(object) = parse_line(line) else {
                report.malformed_lines += 1;
                continue;
            };
            let Some(event) = object.get("event").and_then(Value::as_str) else {
                report.malformed_lines += 1;
                continue;
            };
            *report.event_counts.entry(event.to_owned()).or_default() += 1;
            if duration_event(event) {
                if let Some(duration) = object.get("duration_ms").and_then(Value::as_u64) {
                    durations
                        .entry(event.to_owned())
                        .or_default()
                        .push(duration);
                }
            }
            if object.get("ok").and_then(Value::as_bool) == Some(false) {
                *report.ok_false_counts.entry(event.to_owned()).or_default() += 1;
            }
            if event == "permission_decision" {
                match object.get("allowed").and_then(Value::as_bool) {
                    Some(true) => report.permission_allowed += 1,
                    Some(false) => report.permission_denied += 1,
                    None => {}
                }
            }
        }
        report.duration_ms = durations
            .into_iter()
            .map(|(event, values)| (event, DurationStats::from_values(values)))
            .collect();
        report
    }

    fn to_json(&self) -> Value {
        json!({
            "schema": SCHEMA,
            "lines_scanned": self.lines_scanned,
            "malformed_lines": self.malformed_lines,
            "truncated": self.truncated,
            "turn_count": self.event_counts.get("turn_start").copied().unwrap_or(0),
            "event_counts": self.event_counts,
            "duration_ms": self.duration_ms,
            "ok_false_counts": self.ok_false_counts,
            "permission_decisions": {
                "allowed": self.permission_allowed,
                "denied": self.permission_denied,
            },
        })
    }
}

#[derive(Debug, Eq, PartialEq, serde::Serialize)]
struct DurationStats {
    count: usize,
    max: u64,
    p50: u64,
}

impl DurationStats {
    fn from_values(mut values: Vec<u64>) -> Self {
        values.sort_unstable();
        let count = values.len();
        let p50_index = count.div_ceil(2).saturating_sub(1);
        Self {
            count,
            max: values[count - 1],
            p50: values[p50_index],
        }
    }
}

fn parse_line(line: &str) -> Option<Map<String, Value>> {
    serde_json::from_str::<Value>(line)
        .ok()?
        .as_object()
        .cloned()
}

fn duration_event(event: &str) -> bool {
    matches!(
        event,
        "model_call_end" | "tool_exec_end" | "extension_command_end" | "provenance_append_end"
    )
}

fn reject_unknown_fields(object: &Map<String, Value>) -> Result<(), Error> {
    for key in object.keys() {
        if key != "tail_lines" {
            return Err(input_error(format!("unknown input field `{key}`")));
        }
    }
    Ok(())
}

fn parse_tail_lines(object: &Map<String, Value>) -> Result<u64, Error> {
    let Some(value) = object.get("tail_lines") else {
        return Ok(DEFAULT_TAIL_LINES);
    };
    if value.is_null() {
        return Ok(DEFAULT_TAIL_LINES);
    }
    let Some(parsed) = value.as_u64() else {
        return Err(input_error("tail_lines must be a positive integer"));
    };
    if parsed == 0 {
        return Err(input_error("tail_lines must be greater than zero"));
    }
    Ok(parsed)
}

fn metadata(report: &Value) -> Map<String, Value> {
    Map::from_iter([
        ("schema".to_owned(), Value::String(SCHEMA.to_owned())),
        ("lines_scanned".to_owned(), report["lines_scanned"].clone()),
        ("truncated".to_owned(), report["truncated"].clone()),
    ])
}

fn output(record: &Value, report: &Value) -> Value {
    let field = |name: &str| record.get(name).cloned().unwrap_or(Value::Null);
    json!({
        "persisted_event_id": field("persisted_event_id"),
        "relative_path": field("relative_path"),
        "sha256": field("sha256"),
        "byte_len": field("byte_len"),
        "lines_scanned": report["lines_scanned"],
        "malformed_lines": report["malformed_lines"],
        "truncated": report["truncated"],
        "turn_count": report["turn_count"],
    })
}

fn input_error(message: impl Into<String>) -> Error {
    Error::Command(message.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(value: Value) -> String {
        serde_json::to_string(&value).expect("line json")
    }

    #[test]
    fn aggregates_synthetic_diagnostics_lines() {
        let lines = vec![
            line(json!({"event":"turn_start"})),
            line(json!({"event":"model_call_end","duration_ms":30,"ok":true,"model":"fixture"})),
            line(
                json!({"event":"model_call_end","duration_ms":10,"ok":false,"provider":"fixture"}),
            ),
            line(json!({"event":"tool_exec_end","duration_ms":8,"ok":false,"tool":"read_file"})),
            line(json!({"event":"permission_decision","allowed":true,"capability":"fs-read"})),
            line(json!({"event":"permission_decision","allowed":false,"capability":"fs-write"})),
            "not json".to_owned(),
        ];

        let report = Report::from_lines(&lines, true).to_json();

        assert_eq!(report["schema"], SCHEMA);
        assert_eq!(report["lines_scanned"], json!(7));
        assert_eq!(report["malformed_lines"], json!(1));
        assert_eq!(report["truncated"], json!(true));
        assert_eq!(report["turn_count"], json!(1));
        assert_eq!(report["event_counts"]["model_call_end"], json!(2));
        assert_eq!(report["duration_ms"]["model_call_end"]["count"], json!(2));
        assert_eq!(report["duration_ms"]["model_call_end"]["max"], json!(30));
        assert_eq!(report["duration_ms"]["model_call_end"]["p50"], json!(10));
        assert_eq!(report["ok_false_counts"]["model_call_end"], json!(1));
        assert_eq!(report["ok_false_counts"]["tool_exec_end"], json!(1));
        assert_eq!(report["permission_decisions"]["allowed"], json!(1));
        assert_eq!(report["permission_decisions"]["denied"], json!(1));
        assert!(!serde_json::to_string(&report)
            .expect("report json")
            .contains("read_file"));
    }

    #[test]
    fn null_input_uses_defaults() {
        assert_eq!(
            ReportInput::parse(&Value::Null).expect("null input"),
            ReportInput::default()
        );
        assert_eq!(ReportInput::default().tail_lines, DEFAULT_TAIL_LINES);
    }

    #[test]
    fn empty_object_uses_default_tail_lines() {
        let input = ReportInput::parse(&json!({})).expect("empty object");
        assert_eq!(input.tail_lines, DEFAULT_TAIL_LINES);
    }

    #[test]
    fn tail_lines_is_honored() {
        let input = ReportInput::parse(&json!({"tail_lines": 128})).expect("tail_lines");
        assert_eq!(input.tail_lines, 128);
    }

    #[test]
    fn unknown_input_field_is_rejected() {
        let error = ReportInput::parse(&json!({"tail_lines": 1, "path": "nope"}))
            .expect_err("unknown field");
        assert!(error.to_string().contains("unknown input field `path`"));
    }

    #[test]
    fn non_object_input_is_rejected() {
        let error = ReportInput::parse(&json!("nope")).expect_err("non-object");
        assert!(error
            .to_string()
            .contains("diagnostics-report input must be a JSON object"));
    }

    #[test]
    fn zero_and_non_integer_tail_lines_are_rejected() {
        let zero = ReportInput::parse(&json!({"tail_lines": 0})).expect_err("zero");
        assert!(zero.to_string().contains("must be greater than zero"));
        let text = ReportInput::parse(&json!({"tail_lines": "many"})).expect_err("string");
        assert!(text.to_string().contains("must be a positive integer"));
    }
}
