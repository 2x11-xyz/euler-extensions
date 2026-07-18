//! Euler session-export extension over the managed-process protocol.
//!
//! A faithful port of the bundled in-process extension: it observes
//! provenance through the host and emits bytes through artifact writes,
//! nothing else. Input, artifact, and result shapes are unchanged
//! (`euler.session-export.v1`).

use euler_managed_process_sdk::{
    serve, ArtifactWrite, CommandContext, Error, Handler, ProvenanceQuery,
};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

const DISPLAY_NAME: &str = "Session Export";
const COMMAND_NAME: &str = "session-export";
const DEFAULT_LIMIT: u64 = 64;
const SCHEMA_NAME: &str = "euler.session-export.v1";
const MEDIA_TYPE_JSON: &str = "application/json";

fn main() {
    let mut handlers: BTreeMap<String, Handler<std::io::StdinLock<'static>, std::io::Stdout>> =
        BTreeMap::new();
    handlers.insert(COMMAND_NAME.to_owned(), Box::new(execute));
    serve(handlers);
}

fn execute<R: std::io::BufRead, W: std::io::Write>(
    context: &CommandContext,
    host: &mut euler_managed_process_sdk::Host<'_, R, W>,
) -> Result<Value, Error> {
    let input = ExportInput::parse(&context.input)?;
    let page = host.query_provenance(&input.query())?;
    let events = page
        .get("events")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let source_event_ids: Vec<String> = events
        .iter()
        .filter_map(|event| event.get("id").and_then(Value::as_str))
        .map(str::to_owned)
        .collect();
    let page_field = |name: &str| page.get(name).cloned().unwrap_or(Value::Null);
    let metadata = Map::from_iter([
        ("schema".to_owned(), json!(SCHEMA_NAME)),
        ("event_count".to_owned(), json!(events.len())),
        ("truncated".to_owned(), page_field("truncated")),
        ("applied_limit".to_owned(), page_field("applied_limit")),
        (
            "applied_scan_limit".to_owned(),
            page_field("applied_scan_limit"),
        ),
        ("scanned_events".to_owned(), page_field("scanned_events")),
        (
            "watermark_event_id".to_owned(),
            page_field("watermark_event_id"),
        ),
        (
            "next_after_event_id".to_owned(),
            page_field("next_after_event_id"),
        ),
    ]);
    let artifact = json!({
        "schema": SCHEMA_NAME,
        "events": events,
        "truncated": page_field("truncated"),
        "applied_limit": page_field("applied_limit"),
        "applied_scan_limit": page_field("applied_scan_limit"),
        "scanned_events": page_field("scanned_events"),
        "watermark_event_id": page_field("watermark_event_id"),
        "next_after_event_id": page_field("next_after_event_id"),
    });
    let bytes = serde_json::to_vec(&artifact).map_err(|error| Error::Command(error.to_string()))?;
    let record = host.write_artifact(&ArtifactWrite {
        display_name: DISPLAY_NAME.to_owned(),
        media_type: MEDIA_TYPE_JSON.to_owned(),
        bytes,
        source_event_ids,
        metadata,
    })?;
    let record_field = |name: &str| record.get(name).cloned().unwrap_or(Value::Null);

    Ok(json!({
        "persisted_event_id": record_field("persisted_event_id"),
        "relative_path": record_field("relative_path"),
        "sha256": record_field("sha256"),
        "byte_len": record_field("byte_len"),
        "event_count": events.len(),
        "truncated": page_field("truncated"),
        "applied_limit": page_field("applied_limit"),
        "applied_scan_limit": page_field("applied_scan_limit"),
        "scanned_events": page_field("scanned_events"),
        "watermark_event_id": page_field("watermark_event_id"),
        "next_after_event_id": page_field("next_after_event_id"),
    }))
}

#[derive(Debug, PartialEq)]
struct ExportInput {
    limit: u64,
    scan_limit: Option<u64>,
    after_event_id: Option<String>,
    kinds: Vec<String>,
}

impl ExportInput {
    fn parse(value: &Value) -> Result<Self, Error> {
        if value.is_null() {
            return Ok(Self::default());
        }
        let object = value
            .as_object()
            .ok_or_else(|| input_error("session-export input must be a JSON object"))?;
        reject_unknown_fields(object)?;
        Ok(Self {
            limit: parse_limit(object)?,
            scan_limit: parse_optional_positive(object, "scan_limit")?,
            after_event_id: optional_string(object, "after_event_id")?,
            kinds: optional_string_array(object, "kinds")?,
        })
    }

    fn query(&self) -> ProvenanceQuery {
        let mut query = ProvenanceQuery {
            limit: self.limit,
            ..ProvenanceQuery::default()
        };
        if let Some(scan_limit) = self.scan_limit {
            query.scan_limit = scan_limit;
        }
        query.after_event_id = self.after_event_id.clone();
        query.kinds = self.kinds.clone();
        query
    }
}

impl Default for ExportInput {
    fn default() -> Self {
        Self {
            limit: DEFAULT_LIMIT,
            scan_limit: None,
            after_event_id: None,
            kinds: Vec::new(),
        }
    }
}

fn reject_unknown_fields(object: &Map<String, Value>) -> Result<(), Error> {
    for key in object.keys() {
        if !matches!(
            key.as_str(),
            "limit" | "scan_limit" | "after_event_id" | "kinds"
        ) {
            return Err(input_error(format!("unknown input field `{key}`")));
        }
    }
    Ok(())
}

fn parse_limit(object: &Map<String, Value>) -> Result<u64, Error> {
    match object.get("limit") {
        None | Some(Value::Null) => Ok(DEFAULT_LIMIT),
        Some(value) => match value.as_u64() {
            Some(limit) if limit > 0 => Ok(limit),
            Some(_) => Err(input_error("limit must be greater than zero")),
            None => Err(input_error("limit must be a positive integer")),
        },
    }
}

fn parse_optional_positive(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<Option<u64>, Error> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => match value.as_u64() {
            Some(parsed) if parsed > 0 => Ok(Some(parsed)),
            Some(_) => Err(input_error(format!("{field} must be greater than zero"))),
            None => Err(input_error(format!("{field} must be a positive integer"))),
        },
    }
}

fn optional_string(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<Option<String>, Error> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_str()
            .map(|value| Some(value.to_owned()))
            .ok_or_else(|| input_error(format!("{field} must be a string"))),
    }
}

fn optional_string_array(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<Vec<String>, Error> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(value) => value
            .as_array()
            .ok_or_else(|| input_error(format!("{field} must be an array of strings")))?
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .map(str::to_owned)
                    .ok_or_else(|| input_error(format!("{field} must be an array of strings")))
            })
            .collect(),
    }
}

fn input_error(message: impl Into<String>) -> Error {
    Error::Command(message.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn null_input_uses_defaults() {
        assert_eq!(
            ExportInput::parse(&Value::Null).unwrap(),
            ExportInput::default()
        );
    }

    #[test]
    fn unknown_fields_are_rejected() {
        let error = ExportInput::parse(&json!({"nope": 1})).unwrap_err();
        assert!(error.to_string().contains("unknown input field `nope`"));
    }

    #[test]
    fn zero_and_non_integer_limits_are_rejected() {
        assert!(ExportInput::parse(&json!({"limit": 0})).is_err());
        assert!(ExportInput::parse(&json!({"limit": "many"})).is_err());
        assert!(ExportInput::parse(&json!({"scan_limit": 0})).is_err());
    }

    #[test]
    fn full_input_maps_onto_the_query() {
        let input = ExportInput::parse(&json!({
            "limit": 5,
            "scan_limit": 50,
            "after_event_id": "e9",
            "kinds": ["user.message"],
        }))
        .unwrap();
        let query = input.query();
        assert_eq!(query.limit, 5);
        assert_eq!(query.scan_limit, 50);
        assert_eq!(query.after_event_id.as_deref(), Some("e9"));
        assert_eq!(query.kinds, vec!["user.message"]);
    }
}
