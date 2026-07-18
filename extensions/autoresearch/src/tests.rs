//! Pure-logic unit tests ported from the bundled crate's `lib_test.rs`.
//!
//! The bundled tests drove `ExtensionCommand::execute` against a `MockHost`
//! implementing the typed `HostApi`. The managed-process `Host` is a concrete
//! wire client, not a trait, so these tests exercise the same logic through the
//! pure functions the handlers delegate to: input parsing/query mapping,
//! `build_objective_brief` (given a provenance page value), and
//! `build_objective_report` (given a page value and spawn id). The host round
//! trip itself is covered by the end-to-end verification against `euler-host`.

use super::*;

fn event(id: &str, parent: Option<&str>, kind: &str, payload: Value) -> Value {
    json!({
        "v": 1,
        "id": id,
        "ts": "2026-07-05T00:00:00.000Z",
        "session": "session",
        "agent": "agent",
        "parent": parent,
        "kind": kind,
        "payload": payload,
        "blobs": {},
    })
}

fn page(events: Vec<Value>) -> Value {
    let watermark = events.last().map(|event| event_id(event).to_owned());
    let count = events.len();
    json!({
        "events": events,
        "applied_limit": 64,
        "applied_scan_limit": 1024,
        "scanned_events": count,
        "watermark_event_id": watermark,
        "next_after_event_id": Value::Null,
        "truncated": false,
    })
}

fn source_event(id: &str) -> Value {
    event(
        id,
        None,
        KIND_USER_MESSAGE,
        json!({"content": "source evidence"}),
    )
}

fn spawn_and_result(output: &str) -> (Value, Value) {
    let spawn = event(
        "event-spawn",
        None,
        KIND_AGENT_SPAWN,
        json!({"persona": OBJECTIVE_PERSONA}),
    );
    let result = event(
        "event-result",
        Some("event-spawn"),
        KIND_AGENT_RESULT,
        json!({
            "spawn_event_id": "event-spawn",
            "ok": true,
            "summary": "planned",
            "output": output,
        }),
    );
    (spawn, result)
}

fn valid_objective_json(event_id: &str) -> String {
    json!({
        "schema": OBJECTIVE_SCHEMA,
        "objectives": [{
            "id": "obj-1",
            "title": "Tighten objective validation",
            "rationale": "The log shows validation work is active.",
            "evidence_refs": [{"event_id": event_id, "payload_pointer": "/payload/content"}],
            "expected_outcome": "A validated next objective artifact exists.",
            "acceptance_checks": ["cargo test"]
        }],
        "dead_ends_to_avoid": [{
            "summary": "Do not add web research features.",
            "evidence_refs": [{"event_id": event_id, "payload_pointer": "/payload/content"}]
        }],
        "recommended_objective_id": "obj-1",
        "confidence": {"level": "medium", "score": 0.7}
    })
    .to_string()
}

fn objective_json_with_dead_end_ref(event_id: &str) -> String {
    json!({
        "schema": OBJECTIVE_SCHEMA,
        "objectives": [{
            "id": "obj-1",
            "title": "Tighten objective validation",
            "rationale": "The log shows validation work is active.",
            "evidence_refs": [{"event_id": "event-spawn", "payload_pointer": "/payload/task"}],
            "expected_outcome": "A validated next objective artifact exists.",
            "acceptance_checks": ["cargo test"]
        }],
        "dead_ends_to_avoid": [{
            "summary": "Do not add web research features.",
            "evidence_refs": [{"event_id": event_id, "payload_pointer": "/payload/content"}]
        }],
        "recommended_objective_id": "obj-1",
        "confidence": {"level": "medium", "score": 0.7}
    })
    .to_string()
}

#[test]
fn objective_brief_outputs_agent_task_shape_and_window_watermark() {
    let source = event(
        "event-user",
        None,
        KIND_USER_MESSAGE,
        json!({"content": "find the next useful slice"}),
    );
    let page = page(vec![source]);

    let output = build_objective_brief(&ObjectiveBriefInput::default(), &page).expect("brief");

    assert_eq!(output["schema"], json!(OBJECTIVE_BRIEF_SCHEMA));
    assert_eq!(output["persona"], json!(OBJECTIVE_PERSONA));
    assert_eq!(output["provider"], json!(""));
    assert_eq!(output["model"], json!(""));
    assert_eq!(output["capabilities"], json!([]));
    assert_eq!(output["budget"]["max_turns"], json!(1));
    assert_eq!(output["budget"]["max_tool_calls"], json!(0));
    assert_eq!(output["budget"]["max_tokens"], json!(DEFAULT_MAX_TOKENS));
    assert_eq!(output["watermark_event_id"], json!("event-user"));
    assert!(output["system_prompt"]
        .as_str()
        .expect("system prompt")
        .contains(OBJECTIVE_SCHEMA));
    assert!(output["task"]
        .as_str()
        .expect("task")
        .contains("event-user user.message"));
    assert!(output["task"].as_str().expect("task").len() <= MAX_TASK_BYTES);
}

#[test]
fn objective_brief_rejects_empty_window_and_unknown_input() {
    let empty = build_objective_brief(&ObjectiveBriefInput::default(), &page(Vec::new()))
        .expect_err("empty window");
    assert!(empty.to_string().contains("found no events"));

    let unknown =
        ObjectiveBriefInput::parse(&json!({"limit": 1, "path": "/tmp/nope"})).expect_err("unknown");
    assert!(unknown.to_string().contains("unknown input field `path`"));
}

#[test]
fn objective_brief_maps_limit_scan_and_after_onto_query_and_budget() {
    let input = ObjectiveBriefInput::parse(&json!({
        "limit": 10,
        "scan_limit": 20,
        "after_event_id": "event-cursor",
        "max_tokens": 4096
    }))
    .expect("input");
    let query = input.query();
    assert_eq!(query.limit, 10);
    assert_eq!(query.scan_limit, 20);
    assert_eq!(query.after_event_id.as_deref(), Some("event-cursor"));

    // A page carrying its applied window flows through to objective_window.
    let mut page = page(vec![event("event-a", None, KIND_USER_MESSAGE, json!({}))]);
    page["applied_limit"] = json!(10);
    let output = build_objective_brief(&input, &page).expect("brief");
    assert_eq!(output["budget"]["max_tokens"], json!(4096));
    assert_eq!(output["objective_window"]["applied_limit"], json!(10));
    assert_eq!(output["objective_window"]["limit"], json!(10));
    assert_eq!(output["objective_window"]["scan_limit"], json!(20));
}

#[test]
fn objective_brief_defaults_and_zero_and_non_integer_are_rejected() {
    assert_eq!(
        ObjectiveBriefInput::parse(&Value::Null).expect("null"),
        ObjectiveBriefInput::default()
    );
    assert!(ObjectiveBriefInput::parse(&json!({"limit": 0})).is_err());
    assert!(ObjectiveBriefInput::parse(&json!({"limit": "many"})).is_err());
    assert!(ObjectiveBriefInput::parse(&json!({"scan_limit": 0})).is_err());
}

#[test]
fn objective_report_persists_valid_objective_and_publishes_slot() {
    let (spawn, result) = spawn_and_result(&valid_objective_json("event-real"));
    let page = page(vec![source_event("event-real"), spawn, result]);

    let plan = build_objective_report(&page, "event-spawn").expect("report");
    let artifact: Value = serde_json::from_slice(&plan.bytes).expect("artifact json");

    assert_eq!(plan.recommended_objective_id, json!("obj-1"));
    assert_eq!(plan.result_event_id, "event-result");
    assert_eq!(artifact["schema"], json!(OBJECTIVE_SCHEMA));
    assert_eq!(plan.metadata["schema"], json!(OBJECTIVE_SCHEMA));
    assert_eq!(plan.metadata["recommended_objective_id"], json!("obj-1"));
    assert_eq!(plan.metadata["objective_count"], json!(1));
    assert!(plan
        .slot_text
        .contains("OBJECTIVE: Tighten objective validation"));
    assert!(plan.slot_text.contains("DEAD_ENDS_TO_AVOID: 1"));
}

#[test]
fn objective_report_rejects_malformed_companion_json_and_wrong_schema() {
    let (spawn, result) = spawn_and_result("not json");
    let malformed =
        build_objective_report(&page(vec![spawn, result]), "event-spawn").expect_err("malformed");
    assert!(malformed.to_string().contains("not valid JSON"));

    let (spawn, result) = spawn_and_result(
        &json!({
            "schema": "wrong.schema",
            "objectives": [],
            "dead_ends_to_avoid": [],
            "recommended_objective_id": "obj-1",
            "confidence": {"level": "low", "score": 0.1}
        })
        .to_string(),
    );
    let wrong_schema =
        build_objective_report(&page(vec![spawn, result]), "event-spawn").expect_err("schema");
    assert!(wrong_schema.to_string().contains("schema must be"));
}

#[test]
fn objective_report_rejects_invented_evidence_ref_event_id() {
    let (spawn, result) = spawn_and_result(&valid_objective_json("invented-event-id"));
    let error =
        build_objective_report(&page(vec![spawn, result]), "event-spawn").expect_err("invented");
    let message = error.to_string();
    assert!(message.contains("objective `obj-1`"));
    assert!(message.contains("invented-event-id"));
    assert!(message.contains("widen the window with limit/scan_limit/after_event_id"));
}

#[test]
fn objective_report_rejects_evidence_ref_outside_report_window() {
    // Window honesty, not global existence: objective-report validates refs only
    // against the bounded page it queried. A real event outside that page fails
    // until the operator widens or moves the report window to include it.
    let (spawn, result) = spawn_and_result(&objective_json_with_dead_end_ref("event-outside"));
    let error =
        build_objective_report(&page(vec![spawn, result]), "event-spawn").expect_err("outside");
    let message = error.to_string();
    assert!(message.contains("dead_end `dead_end[0]`"));
    assert!(message.contains("event-outside"));
    assert!(message.contains("bounded provenance window only"));
}

#[test]
fn objective_report_rejects_unpaired_spawn_and_unknown_input() {
    let spawn = event(
        "event-spawn",
        None,
        KIND_AGENT_SPAWN,
        json!({"persona": OBJECTIVE_PERSONA}),
    );
    let unpaired = build_objective_report(&page(vec![spawn]), "event-spawn").expect_err("unpaired");
    assert!(unpaired.to_string().contains("widen the window"));

    let unknown =
        ObjectiveReportInput::parse(&json!({"spawn_event_id": "event-spawn", "path": "nope"}))
            .expect_err("unknown input");
    assert!(unknown.to_string().contains("unknown input field `path`"));
}

#[test]
fn objective_report_rejects_spawn_with_wrong_persona() {
    let spawn = event(
        "event-spawn",
        None,
        KIND_AGENT_SPAWN,
        json!({"persona": "some-other-persona"}),
    );
    let result = event(
        "event-result",
        Some("event-spawn"),
        KIND_AGENT_RESULT,
        json!({"spawn_event_id": "event-spawn", "ok": true, "output": "{}"}),
    );
    let error =
        build_objective_report(&page(vec![spawn, result]), "event-spawn").expect_err("persona");
    assert!(error
        .to_string()
        .contains("is not an autoresearch objective brief"));
}

#[test]
fn objective_report_input_maps_onto_query() {
    let input = ObjectiveReportInput::parse(&json!({
        "spawn_event_id": "event-spawn",
        "limit": 32,
        "scan_limit": 64,
        "after_event_id": "cursor"
    }))
    .expect("input");
    let query = input.query();
    assert_eq!(input.spawn_event_id, "event-spawn");
    assert_eq!(query.limit, 32);
    assert_eq!(query.scan_limit, 64);
    assert_eq!(query.after_event_id.as_deref(), Some("cursor"));
}

#[test]
fn objective_brief_drops_oldest_events_to_fit_the_agent_task_bound() {
    let mut events = Vec::new();
    for index in 0..400 {
        events.push(event(
            &format!("event-{index:04}"),
            None,
            KIND_USER_MESSAGE,
            json!({"content": "long analysis narrative ".repeat(8)}),
        ));
    }
    let output =
        build_objective_brief(&ObjectiveBriefInput::default(), &page(events)).expect("brief");
    let task = output["task"].as_str().expect("task");
    assert!(
        task.len() <= MAX_TASK_BYTES,
        "task fits the AgentTask bound: {} bytes",
        task.len()
    );
    let listed = output["listed_event_count"].as_u64().expect("listed");
    let omitted = output["omitted_event_count"].as_u64().expect("omitted");
    assert!(omitted > 0, "oversized window reports omissions");
    assert!(listed > 0);
    assert!(task.contains("event-0399"), "newest events survive");
    assert!(!task.contains("event-0000"), "oldest events are dropped");
}
