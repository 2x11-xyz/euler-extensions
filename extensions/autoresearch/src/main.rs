//! Euler autoresearch extension over the managed-process protocol.
//!
//! A faithful port of the bundled in-process extension. It observes provenance
//! through the host, builds a companion AgentTask brief for choosing the next
//! repo-directed research objective (`objective-brief`), and persists a
//! companion-produced objective as an artifact plus a context slot
//! (`objective-report`). Input validation, artifact schema, and result shapes
//! are unchanged (`euler.autoresearch.objective_brief.v1` /
//! `euler.autoresearch.objective.v1`).
//!
//! The bundled crate depended on typed `EventEnvelope`/`ProvenancePage` values
//! from euler-event/euler-sdk. The managed-process wire speaks JSON, so this
//! port reads events and pages as `serde_json::Value` against the exact field
//! names the host serializes.

use euler_managed_process_sdk::{
    serve, ArtifactWrite, CommandContext, Error, Handler, ProvenanceQuery,
};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

const OBJECTIVE_BRIEF_COMMAND: &str = "objective-brief";
const OBJECTIVE_REPORT_COMMAND: &str = "objective-report";
const DISPLAY_NAME: &str = "Autoresearch";
const OBJECTIVE_BRIEF_SCHEMA: &str = "euler.autoresearch.objective_brief.v1";
const OBJECTIVE_SCHEMA: &str = "euler.autoresearch.objective.v1";
const OBJECTIVE_MEDIA_TYPE: &str = "application/vnd.euler.autoresearch.objective.v1+json";
const DEFAULT_LIMIT: u64 = 64;
const DEFAULT_REPORT_LIMIT: u64 = 128;
// AgentBudget max_tokens counts input + output. The planner sees a bounded
// provenance listing plus must produce evidence-backed objective JSON; match
// the Causal DAG observer default so output has headroom after input context.
const DEFAULT_MAX_TOKENS: u64 = 24_576;
// Mirrors euler_agents::MAX_TASK_BYTES (8 * 1024). The task must fit the real
// AgentTask bound the host enforces on companion runs; a larger local constant
// produced briefs that companion_run rejected.
const MAX_TASK_BYTES: usize = 8 * 1024;
const MAX_SYSTEM_PROMPT_BYTES: usize = 8 * 1024;
const EXTRACT_CHARS: usize = 240;
const OBJECTIVE_SLOT_NAME: &str = "objective";
const OBJECTIVE_PERSONA: &str = "autoresearch-planner";

// Event kinds, mirroring euler_event::EventKind wire strings.
const KIND_USER_MESSAGE: &str = "user.message";
const KIND_ASSISTANT_MESSAGE: &str = "assistant.message";
const KIND_ASSISTANT_ACTIVITY: &str = "assistant.activity";
const KIND_PLAN_UPDATE: &str = "plan.update";
const KIND_TOOL_CALL: &str = "tool.call";
const KIND_TOOL_RESULT: &str = "tool.result";
const KIND_CHECK_STARTED: &str = "check.started";
const KIND_CHECK_RESULT: &str = "check.result";
const KIND_EXTENSION_ARTIFACT: &str = "extension.artifact";
const KIND_AGENT_SPAWN: &str = "agent.spawn";
const KIND_AGENT_RESULT: &str = "agent.result";

fn main() {
    let mut handlers: BTreeMap<String, Handler<std::io::StdinLock<'static>, std::io::Stdout>> =
        BTreeMap::new();
    handlers.insert(OBJECTIVE_BRIEF_COMMAND.to_owned(), Box::new(execute_brief));
    handlers.insert(
        OBJECTIVE_REPORT_COMMAND.to_owned(),
        Box::new(execute_report),
    );
    serve(handlers);
}

fn execute_brief<R: std::io::BufRead, W: std::io::Write>(
    context: &CommandContext,
    host: &mut euler_managed_process_sdk::Host<'_, R, W>,
) -> Result<Value, Error> {
    let input = ObjectiveBriefInput::parse(&context.input)?;
    let page = host.query_provenance(&input.query())?;
    build_objective_brief(&input, &page)
}

fn execute_report<R: std::io::BufRead, W: std::io::Write>(
    context: &CommandContext,
    host: &mut euler_managed_process_sdk::Host<'_, R, W>,
) -> Result<Value, Error> {
    let input = ObjectiveReportInput::parse(&context.input)?;
    let page = host.query_provenance(&input.query())?;
    let plan = build_objective_report(&page, &input.spawn_event_id)?;
    let record = host.write_artifact(&ArtifactWrite {
        display_name: DISPLAY_NAME.to_owned(),
        media_type: OBJECTIVE_MEDIA_TYPE.to_owned(),
        bytes: plan.bytes,
        source_event_ids: vec![plan.result_event_id.clone()],
        metadata: plan.metadata,
    })?;
    host.update_context_slot(OBJECTIVE_SLOT_NAME, &plan.slot_text)?;
    let record_field = |name: &str| record.get(name).cloned().unwrap_or(Value::Null);
    Ok(json!({
        "persisted_event_id": record_field("persisted_event_id"),
        "relative_path": record_field("relative_path"),
        "sha256": record_field("sha256"),
        "byte_len": record_field("byte_len"),
        "result_event_id": plan.result_event_id,
        "recommended_objective_id": plan.recommended_objective_id,
        "slot_published": true,
    }))
}

// ---------------------------------------------------------------------------
// objective-brief
// ---------------------------------------------------------------------------

fn build_objective_brief(input: &ObjectiveBriefInput, page: &Value) -> Result<Value, Error> {
    let events = page_events(page);
    if events.is_empty() {
        return Err(input_error(
            "autoresearch objective-brief found no events in bounded provenance window",
        ));
    }
    let watermark_event_id = page
        .get("watermark_event_id")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .or_else(|| events.last().map(|event| event_id(event).to_owned()))
        .or_else(|| input.after_event_id.clone())
        .ok_or_else(|| input_error("autoresearch objective-brief has no watermark event"))?;
    let (task, omitted_event_count) = objective_task(events)?;
    let system_prompt = objective_system_prompt()?;
    Ok(objective_brief_output(
        input,
        task,
        system_prompt,
        watermark_event_id,
        page,
        events.len(),
        omitted_event_count,
    ))
}

#[allow(clippy::too_many_arguments)]
fn objective_brief_output(
    input: &ObjectiveBriefInput,
    task: String,
    system_prompt: String,
    watermark_event_id: String,
    page: &Value,
    event_count: usize,
    omitted_event_count: usize,
) -> Value {
    let page_field = |name: &str| page.get(name).cloned().unwrap_or(Value::Null);
    json!({
        "schema": OBJECTIVE_BRIEF_SCHEMA,
        "task": task,
        "persona": OBJECTIVE_PERSONA,
        "provider": "",
        "model": "",
        "system_prompt": system_prompt,
        "capabilities": [],
        "budget": {"max_turns": 1, "max_tool_calls": 0, "max_tokens": input.max_tokens},
        "objective_window": {
            "limit": input.limit,
            "scan_limit": input.scan_limit,
            "after_event_id": input.after_event_id,
            "watermark_event_id": watermark_event_id,
            "applied_limit": page_field("applied_limit"),
            "applied_scan_limit": page_field("applied_scan_limit"),
            "scanned_events": page_field("scanned_events"),
            "truncated": page_field("truncated"),
            "next_after_event_id": page_field("next_after_event_id"),
        },
        "watermark_event_id": watermark_event_id,
        "listed_event_count": event_count - omitted_event_count,
        "omitted_event_count": omitted_event_count,
    })
}

fn objective_task(events: &[Value]) -> Result<(String, usize), Error> {
    let header = [
        "Choose the next repo-directed research objective from these Euler events.".to_owned(),
        "Cite only listed event ids in evidence_refs.".to_owned(),
    ];
    // The task must fit the real AgentTask bound (MAX_TASK_BYTES). Keep the
    // newest events that fit and report how many older ones were dropped so the
    // operator can narrow the window deliberately instead of the brief failing
    // after the fact.
    let budget = MAX_TASK_BYTES - header.iter().map(|line| line.len() + 1).sum::<usize>();
    let mut kept = VecDeque::new();
    let mut used = 0usize;
    for event in events.iter().rev() {
        let line = event_line(event);
        let cost = line.len() + 1;
        if used + cost > budget {
            break;
        }
        used += cost;
        kept.push_front(line);
    }
    if kept.is_empty() {
        return Err(input_error(
            "objective-brief window has no event line that fits the task budget",
        ));
    }
    let omitted = events.len() - kept.len();
    let mut lines = header.to_vec();
    lines.extend(kept);
    Ok((lines.join("\n"), omitted))
}

fn event_line(event: &Value) -> String {
    format!(
        "{} {} {}",
        event_id(event),
        event_kind(event),
        truncate_chars(&normalize_extract(&payload_extract(event)), EXTRACT_CHARS)
    )
}

fn payload_extract(event: &Value) -> String {
    let payload = event_payload(event);
    match event_kind(event) {
        KIND_USER_MESSAGE | KIND_ASSISTANT_MESSAGE | KIND_ASSISTANT_ACTIVITY => {
            first_string(payload, &["content", "summary", "message"])
        }
        KIND_PLAN_UPDATE => first_string(payload, &["content", "summary", "plan"]),
        KIND_TOOL_CALL => join_parts(&[
            field_part(payload, "name"),
            value_part(payload, "input", "input"),
        ]),
        KIND_TOOL_RESULT => join_parts(&[
            field_part(payload, "name"),
            value_part(payload, "ok", "ok"),
            field_part(payload, "error"),
            field_part(payload, "output"),
        ]),
        KIND_CHECK_STARTED | KIND_CHECK_RESULT => join_parts(&[
            field_part(payload, "name"),
            value_part(payload, "ok", "ok"),
            field_part(payload, "command"),
            field_part(payload, "output"),
            field_part(payload, "error"),
        ]),
        KIND_EXTENSION_ARTIFACT => join_parts(&[
            field_part(payload, "extension_id"),
            field_part(payload, "media_type"),
            metadata_schema_part(payload),
        ]),
        _ => payload.to_string(),
    }
}

fn objective_system_prompt() -> Result<String, Error> {
    let prompt = [
        "You are the Autoresearch planner for Euler.",
        "Return exactly one raw JSON object. Do not use markdown fences.",
        "Use schema euler.autoresearch.objective.v1 and this exact top-level shape:",
        "{\"schema\":\"euler.autoresearch.objective.v1\",\"objectives\":[],\"dead_ends_to_avoid\":[],\"recommended_objective_id\":\"objective-id\",\"confidence\":{\"level\":\"medium\",\"score\":0.5}}",
        "Each objective has: id, title, rationale, evidence_refs, expected_outcome, acceptance_checks.",
        "Each dead_ends_to_avoid item has: summary, evidence_refs.",
        "Each evidence ref has exactly: event_id, payload_pointer.",
        "Every evidence_ref.event_id must be one of the event ids listed in the task.",
        "Do not invent event ids, payload pointers, files, web facts, literature facts, or tools.",
        "Use JSON Pointers against the event object, usually /payload/content, /payload/output, or /payload/error.",
        "Objectives must be repo-directed next work for the current Euler session.",
        "Acceptance checks must be concrete commands, inspections, or review steps the operator can run.",
        "Set recommended_objective_id to one objective id from objectives.",
        "Confidence level is high, medium, or low; score is 0.0 through 1.0.",
    ]
    .join("\n");
    if prompt.len() > MAX_SYSTEM_PROMPT_BYTES {
        return Err(input_error("objective system_prompt exceeds 8192 bytes"));
    }
    Ok(prompt)
}

// ---------------------------------------------------------------------------
// objective-report
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct ObjectiveReportPlan {
    bytes: Vec<u8>,
    metadata: Map<String, Value>,
    slot_text: String,
    result_event_id: String,
    recommended_objective_id: Value,
}

fn build_objective_report(
    page: &Value,
    spawn_event_id: &str,
) -> Result<ObjectiveReportPlan, Error> {
    let events = page_events(page);
    let paired = find_paired_result(spawn_event_id, events)?;
    let parsed = parse_objective_output(&paired.output, events)?;
    let bytes = serde_json::to_vec(&parsed).map_err(|error| Error::Command(error.to_string()))?;
    let metadata = objective_metadata(&parsed);
    let slot_text = render_objective_slot(&parsed);
    Ok(ObjectiveReportPlan {
        bytes,
        metadata,
        slot_text,
        result_event_id: paired.result_event_id,
        recommended_objective_id: parsed["recommended_objective_id"].clone(),
    })
}

struct PairedObjectiveResult {
    result_event_id: String,
    output: String,
}

fn find_paired_result(
    spawn_event_id: &str,
    events: &[Value],
) -> Result<PairedObjectiveResult, Error> {
    let spawn = events
        .iter()
        .find(|event| event_kind(event) == KIND_AGENT_SPAWN && event_id(event) == spawn_event_id)
        .ok_or_else(|| {
            input_error(format!(
                "agent.spawn {spawn_event_id} not found in bounded page; widen the window (limit/after_event_id/scan_limit) so the spawn and agent.result pair are inside it"
            ))
        })?;
    if required_payload_string(spawn, "persona")? != OBJECTIVE_PERSONA {
        return Err(input_error(format!(
            "agent.spawn {spawn_event_id} is not an autoresearch objective brief"
        )));
    }
    let mut matches = events.iter().filter(|event| {
        event_kind(event) == KIND_AGENT_RESULT
            && event_payload(event)
                .get("spawn_event_id")
                .and_then(Value::as_str)
                == Some(spawn_event_id)
    });
    let result = matches.next().ok_or_else(|| {
        input_error(format!(
            "agent.result for spawn_event_id {spawn_event_id} not found in bounded page; widen the window (limit/after_event_id/scan_limit) so the spawn and result pair are inside it"
        ))
    })?;
    if matches.next().is_some() {
        return Err(input_error(format!(
            "multiple agent.result events found for spawn_event_id {spawn_event_id}"
        )));
    }
    if event_parent(result) != Some(spawn_event_id) {
        return Err(input_error(format!(
            "agent.result {} parent does not match spawn_event_id",
            event_id(result)
        )));
    }
    if !required_payload_bool(result, "ok")? {
        return Err(input_error(format!(
            "agent.result {} is not successful",
            event_id(result)
        )));
    }
    Ok(PairedObjectiveResult {
        result_event_id: event_id(result).to_owned(),
        output: required_payload_string(result, "output")?.to_owned(),
    })
}

fn parse_objective_output(output: &str, report_window: &[Value]) -> Result<Value, Error> {
    let value: Value = serde_json::from_str(output)
        .map_err(|error| input_error(format!("objective output is not valid JSON: {error}")))?;
    validate_objective(&value)?;
    validate_evidence_refs_in_report_window(&value, report_window)?;
    Ok(value)
}

fn validate_objective(value: &Value) -> Result<(), Error> {
    let object = value
        .as_object()
        .ok_or_else(|| input_error("objective output must be a JSON object"))?;
    require_schema(object)?;
    let objectives = required_array(object, "objectives")?;
    if objectives.is_empty() {
        return Err(input_error("objectives must not be empty"));
    }
    let mut objective_ids = Vec::with_capacity(objectives.len());
    for objective in objectives {
        validate_objective_item(objective, &mut objective_ids)?;
    }
    validate_dead_ends(required_array(object, "dead_ends_to_avoid")?)?;
    let recommended = required_string(object, "recommended_objective_id")?;
    if !objective_ids.iter().any(|id| id == recommended) {
        return Err(input_error(
            "recommended_objective_id must match an objective id",
        ));
    }
    validate_confidence(required_object(object, "confidence")?)
}

fn require_schema(object: &Map<String, Value>) -> Result<(), Error> {
    let schema = required_string(object, "schema")?;
    if schema != OBJECTIVE_SCHEMA {
        return Err(input_error(format!(
            "schema must be {OBJECTIVE_SCHEMA}, got {schema}"
        )));
    }
    Ok(())
}

fn validate_objective_item(value: &Value, objective_ids: &mut Vec<String>) -> Result<(), Error> {
    let object = value
        .as_object()
        .ok_or_else(|| input_error("objectives entries must be JSON objects"))?;
    let id = required_string(object, "id")?;
    if id.is_empty() {
        return Err(input_error("objective id must not be empty"));
    }
    if objective_ids.iter().any(|seen| seen == id) {
        return Err(input_error(format!("duplicate objective id `{id}`")));
    }
    objective_ids.push(id.to_owned());
    required_non_empty_string(object, "title")?;
    required_non_empty_string(object, "rationale")?;
    validate_evidence_refs(required_array(object, "evidence_refs")?)?;
    required_non_empty_string(object, "expected_outcome")?;
    validate_string_array(
        required_array(object, "acceptance_checks")?,
        "acceptance_checks",
    )
}

fn validate_dead_ends(values: &[Value]) -> Result<(), Error> {
    for value in values {
        let object = value
            .as_object()
            .ok_or_else(|| input_error("dead_ends_to_avoid entries must be JSON objects"))?;
        required_non_empty_string(object, "summary")?;
        validate_evidence_refs(required_array(object, "evidence_refs")?)?;
    }
    Ok(())
}

fn validate_evidence_refs(values: &[Value]) -> Result<(), Error> {
    if values.is_empty() {
        return Err(input_error("evidence_refs must not be empty"));
    }
    for value in values {
        let object = value
            .as_object()
            .ok_or_else(|| input_error("evidence_refs entries must be JSON objects"))?;
        required_non_empty_string(object, "event_id")?;
        let pointer = required_string(object, "payload_pointer")?;
        if !pointer.is_empty() && !pointer.starts_with('/') {
            return Err(input_error(
                "payload_pointer must be empty or a JSON Pointer",
            ));
        }
    }
    Ok(())
}

fn validate_string_array(values: &[Value], field: &'static str) -> Result<(), Error> {
    if values.is_empty() {
        return Err(input_error(format!("{field} must not be empty")));
    }
    for value in values {
        if value.as_str().is_none_or(str::is_empty) {
            return Err(input_error(format!(
                "{field} must be a non-empty string array"
            )));
        }
    }
    Ok(())
}

fn validate_confidence(object: &Map<String, Value>) -> Result<(), Error> {
    let level = required_string(object, "level")?;
    if !matches!(level, "high" | "medium" | "low") {
        return Err(input_error("confidence.level must be high, medium, or low"));
    }
    let score = object
        .get("score")
        .and_then(Value::as_f64)
        .ok_or_else(|| input_error("confidence.score must be a number"))?;
    if !(0.0..=1.0).contains(&score) {
        return Err(input_error("confidence.score must be between 0.0 and 1.0"));
    }
    Ok(())
}

fn validate_evidence_refs_in_report_window(
    objective: &Value,
    events: &[Value],
) -> Result<(), Error> {
    let event_ids = events.iter().map(event_id).collect::<BTreeSet<_>>();
    for item in objective["objectives"].as_array().into_iter().flatten() {
        let id = item["id"].as_str().unwrap_or("<invalid-objective>");
        validate_refs_for_owner(
            OwnerKind::Objective,
            id,
            item["evidence_refs"].as_array().into_iter().flatten(),
            &event_ids,
        )?;
    }
    for (index, item) in objective["dead_ends_to_avoid"]
        .as_array()
        .into_iter()
        .flatten()
        .enumerate()
    {
        let id = format!("dead_end[{index}]");
        validate_refs_for_owner(
            OwnerKind::DeadEnd,
            &id,
            item["evidence_refs"].as_array().into_iter().flatten(),
            &event_ids,
        )?;
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum OwnerKind {
    Objective,
    DeadEnd,
}

impl OwnerKind {
    fn label(self) -> &'static str {
        match self {
            Self::Objective => "objective",
            Self::DeadEnd => "dead_end",
        }
    }
}

fn validate_refs_for_owner<'a>(
    owner_kind: OwnerKind,
    owner_id: &str,
    refs: impl Iterator<Item = &'a Value>,
    event_ids: &BTreeSet<&str>,
) -> Result<(), Error> {
    for reference in refs {
        let event_id = reference["event_id"]
            .as_str()
            .unwrap_or("<invalid-event-id>");
        if !event_ids.contains(event_id) {
            return Err(input_error(format!(
                "unknown evidence_ref event_id `{event_id}` in {} `{owner_id}`; objective-report validates refs against its bounded provenance window only; widen the window with limit/scan_limit/after_event_id so cited events are included",
                owner_kind.label()
            )));
        }
    }
    Ok(())
}

fn objective_metadata(objective: &Value) -> Map<String, Value> {
    Map::from_iter([
        (
            "schema".to_owned(),
            Value::String(OBJECTIVE_SCHEMA.to_owned()),
        ),
        (
            "recommended_objective_id".to_owned(),
            objective["recommended_objective_id"].clone(),
        ),
        (
            "objective_count".to_owned(),
            json!(objective["objectives"].as_array().map_or(0, Vec::len)),
        ),
    ])
}

fn render_objective_slot(objective: &Value) -> String {
    let recommended = recommended_objective(objective);
    let title = recommended
        .and_then(|item| item.get("title"))
        .and_then(Value::as_str)
        .unwrap_or("unknown objective");
    let checks = recommended
        .and_then(|item| item.get("acceptance_checks"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .take(3)
        .map(ascii_safe_line)
        .collect::<Vec<_>>();
    let dead_end_count = objective["dead_ends_to_avoid"]
        .as_array()
        .map_or(0, Vec::len);
    let mut lines = vec![
        format!("OBJECTIVE: {}", ascii_safe_line(title)),
        format!("DEAD_ENDS_TO_AVOID: {dead_end_count}"),
    ];
    if !checks.is_empty() {
        lines.push("ACCEPTANCE_CHECKS:".to_owned());
        lines.extend(checks.into_iter().map(|check| format!("- {check}")));
    }
    fit_slot(lines.join("\n"))
}

fn recommended_objective(objective: &Value) -> Option<&Value> {
    let recommended = objective["recommended_objective_id"].as_str()?;
    objective["objectives"]
        .as_array()?
        .iter()
        .find(|item| item["id"].as_str() == Some(recommended))
}

fn ascii_safe_line(value: &str) -> String {
    value
        .chars()
        .filter_map(|ch| match ch {
            '\n' | '\r' | '\t' => Some(' '),
            ch if ch.is_ascii_graphic() || ch == ' ' => Some(ch),
            _ => None,
        })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn fit_slot(mut value: String) -> String {
    const MAX_SLOT_BYTES: usize = 4096;
    if value.len() <= MAX_SLOT_BYTES {
        return value;
    }
    value.truncate(MAX_SLOT_BYTES);
    while !value.is_char_boundary(value.len()) {
        value.pop();
    }
    value
}

// ---------------------------------------------------------------------------
// input parsing
// ---------------------------------------------------------------------------

#[derive(Debug, Eq, PartialEq)]
struct ObjectiveBriefInput {
    limit: u64,
    scan_limit: Option<u64>,
    after_event_id: Option<String>,
    max_tokens: u64,
}

impl ObjectiveBriefInput {
    fn parse(value: &Value) -> Result<Self, Error> {
        if value.is_null() {
            return Ok(Self::default());
        }
        let object = value.as_object().ok_or_else(|| {
            input_error("autoresearch objective-brief input must be a JSON object")
        })?;
        reject_unknown_fields(
            object,
            &["limit", "scan_limit", "after_event_id", "max_tokens"],
        )?;
        Ok(Self {
            limit: parse_positive_u64(object, "limit", DEFAULT_LIMIT)?,
            scan_limit: parse_optional_positive_u64(object, "scan_limit")?,
            after_event_id: optional_string(object, "after_event_id")?,
            max_tokens: parse_positive_u64(object, "max_tokens", DEFAULT_MAX_TOKENS)?,
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
        query
    }
}

impl Default for ObjectiveBriefInput {
    fn default() -> Self {
        Self {
            limit: DEFAULT_LIMIT,
            scan_limit: None,
            after_event_id: None,
            max_tokens: DEFAULT_MAX_TOKENS,
        }
    }
}

#[derive(Debug, Eq, PartialEq)]
struct ObjectiveReportInput {
    spawn_event_id: String,
    limit: u64,
    scan_limit: Option<u64>,
    after_event_id: Option<String>,
}

impl ObjectiveReportInput {
    fn parse(value: &Value) -> Result<Self, Error> {
        let object = value.as_object().ok_or_else(|| {
            input_error("autoresearch objective-report input must be a JSON object")
        })?;
        reject_unknown_fields(
            object,
            &["spawn_event_id", "limit", "scan_limit", "after_event_id"],
        )?;
        Ok(Self {
            spawn_event_id: required_non_empty_string(object, "spawn_event_id")?,
            limit: parse_positive_u64(object, "limit", DEFAULT_REPORT_LIMIT)?,
            scan_limit: parse_optional_positive_u64(object, "scan_limit")?,
            after_event_id: optional_string(object, "after_event_id")?,
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
        query
    }
}

// ---------------------------------------------------------------------------
// event / payload value helpers
// ---------------------------------------------------------------------------

fn page_events(page: &Value) -> &[Value] {
    page.get("events")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

fn event_id(event: &Value) -> &str {
    event.get("id").and_then(Value::as_str).unwrap_or_default()
}

fn event_kind(event: &Value) -> &str {
    event
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or_default()
}

fn event_parent(event: &Value) -> Option<&str> {
    event.get("parent").and_then(Value::as_str)
}

fn event_payload(event: &Value) -> &Value {
    event.get("payload").unwrap_or(&Value::Null)
}

fn first_string(payload: &Value, keys: &[&str]) -> String {
    keys.iter()
        .find_map(|key| payload.get(*key).and_then(Value::as_str))
        .unwrap_or_default()
        .to_owned()
}

fn field_part(payload: &Value, key: &str) -> Option<String> {
    payload
        .get(key)
        .and_then(Value::as_str)
        .map(|value| format!("{key}={value}"))
}

fn value_part(payload: &Value, key: &str, label: &str) -> Option<String> {
    payload.get(key).map(|value| format!("{label}={value}"))
}

fn metadata_schema_part(payload: &Value) -> Option<String> {
    payload
        .get("metadata")
        .and_then(Value::as_object)
        .and_then(|metadata| metadata.get("schema"))
        .and_then(Value::as_str)
        .map(|schema| format!("schema={schema}"))
}

fn join_parts(parts: &[Option<String>]) -> String {
    parts
        .iter()
        .filter_map(Option::as_deref)
        .collect::<Vec<_>>()
        .join(" ")
}

fn normalize_extract(value: &str) -> String {
    value
        .chars()
        .map(|ch| if ch.is_control() { ' ' } else { ch })
        .collect()
}

fn truncate_chars(value: &str, max_chars: usize) -> String {
    value.chars().take(max_chars).collect()
}

fn required_payload_string<'a>(event: &'a Value, field: &'static str) -> Result<&'a str, Error> {
    event_payload(event)
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| input_error(format!("{} payload missing `{field}`", event_kind(event))))
}

fn required_payload_bool(event: &Value, field: &'static str) -> Result<bool, Error> {
    event_payload(event)
        .get(field)
        .and_then(Value::as_bool)
        .ok_or_else(|| {
            input_error(format!(
                "{} payload `{field}` must be a bool",
                event_kind(event)
            ))
        })
}

// ---------------------------------------------------------------------------
// scalar input helpers
// ---------------------------------------------------------------------------

fn reject_unknown_fields(
    object: &Map<String, Value>,
    allowed: &[&'static str],
) -> Result<(), Error> {
    for key in object.keys() {
        if !allowed.contains(&key.as_str()) {
            return Err(input_error(format!("unknown input field `{key}`")));
        }
    }
    Ok(())
}

fn parse_positive_u64(
    object: &Map<String, Value>,
    field: &'static str,
    default: u64,
) -> Result<u64, Error> {
    let Some(value) = object.get(field) else {
        return Ok(default);
    };
    if value.is_null() {
        return Ok(default);
    }
    let parsed = value
        .as_u64()
        .ok_or_else(|| input_error(format!("{field} must be a positive integer")))?;
    if parsed == 0 {
        return Err(input_error(format!("{field} must be greater than zero")));
    }
    Ok(parsed)
}

fn parse_optional_positive_u64(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<Option<u64>, Error> {
    if object.get(field).is_none_or(Value::is_null) {
        return Ok(None);
    }
    parse_positive_u64(object, field, 1).map(Some)
}

fn optional_string(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<Option<String>, Error> {
    let Some(value) = object.get(field) else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_str()
        .map(|value| Some(value.to_owned()))
        .ok_or_else(|| input_error(format!("{field} must be a string")))
}

fn required_non_empty_string(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<String, Error> {
    let value = required_string(object, field)?.to_owned();
    if value.is_empty() {
        return Err(input_error(format!("{field} must not be empty")));
    }
    Ok(value)
}

fn required_string<'a>(
    object: &'a Map<String, Value>,
    field: &'static str,
) -> Result<&'a str, Error> {
    object
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| input_error(format!("missing or invalid `{field}`")))
}

fn required_array<'a>(
    object: &'a Map<String, Value>,
    field: &'static str,
) -> Result<&'a [Value], Error> {
    object
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| input_error(format!("missing or invalid `{field}`")))
}

fn required_object<'a>(
    object: &'a Map<String, Value>,
    field: &'static str,
) -> Result<&'a Map<String, Value>, Error> {
    object
        .get(field)
        .and_then(Value::as_object)
        .ok_or_else(|| input_error(format!("missing or invalid `{field}`")))
}

fn input_error(message: impl Into<String>) -> Error {
    Error::Command(message.into())
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
