use super::*;
use std::io::Cursor;

type TestReader = Cursor<Vec<u8>>;
type TestWriter = Vec<u8>;

fn script(messages: &[Value]) -> TestReader {
    let mut bytes = Vec::new();
    for message in messages {
        bytes.extend_from_slice(serde_json::to_string(message).unwrap().as_bytes());
        bytes.push(b'\n');
    }
    Cursor::new(bytes)
}

fn written_messages(writer: &[u8]) -> Vec<Value> {
    writer
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice(line).expect("valid written JSON"))
        .collect()
}

fn initialize(id: u64) -> Value {
    json!({
        "jsonrpc": "2.0", "id": id, "method": "initialize",
        "params": {"protocol_versions": [PROTOCOL_VERSION]},
    })
}

fn initialized() -> Value {
    json!({"jsonrpc": "2.0", "method": "initialized"})
}

fn command(id: u64, name: &str, input: Value) -> Value {
    json!({
        "jsonrpc": "2.0", "id": id, "method": "euler/command",
        "params": {"command": name, "input": input},
    })
}

fn shutdown_and_exit(id: u64) -> [Value; 2] {
    [
        json!({"jsonrpc": "2.0", "id": id, "method": "shutdown"}),
        json!({"jsonrpc": "2.0", "method": "exit"}),
    ]
}

#[test]
fn full_lifecycle_with_host_round_trips() {
    let [shutdown, exit] = shutdown_and_exit(3);
    let reader = script(&[
        initialize(1),
        initialized(),
        command(2, "export", json!({"limit": 2})),
        // Host responses to the handler's two requests, in order.
        json!({"jsonrpc": "2.0", "id": "client-1", "result": {
            "events": [{"id": "e1"}], "truncated": false,
        }}),
        json!({"jsonrpc": "2.0", "id": "client-2", "result": {
            "persisted_event_id": "a1", "relative_path": "artifacts/x", "sha256": "h", "byte_len": 2,
        }}),
        json!({"jsonrpc": "2.0", "id": "client-3", "result": {}}),
        shutdown,
        exit,
    ]);
    let mut writer: TestWriter = Vec::new();
    let mut handlers: BTreeMap<String, Handler<TestReader, &mut TestWriter>> = BTreeMap::new();
    handlers.insert(
        "export".to_owned(),
        Box::new(|context, host| {
            assert_eq!(context.input, json!({"limit": 2}));
            let page = host.query_provenance(&ProvenanceQuery {
                limit: 2,
                ..ProvenanceQuery::default()
            })?;
            let record = host.write_artifact(&ArtifactWrite {
                display_name: "Test".to_owned(),
                media_type: "application/json".to_owned(),
                bytes: b"{}".to_vec(),
                source_event_ids: vec!["e1".to_owned()],
                metadata: Map::new(),
            })?;
            host.update_plan_presentation(&PlanPresentation {
                revision: 2,
                status: PlanPresentationStatus::Active,
                explanation: Some("Implement the bridge".to_owned()),
                items: vec![
                    PlanPresentationItem {
                        step: "Inspect".to_owned(),
                        status: PlanItemStatus::Completed,
                    },
                    PlanPresentationItem {
                        step: "Implement".to_owned(),
                        status: PlanItemStatus::InProgress,
                    },
                ],
            })?;
            Ok(json!({
                "events": page.get("events").cloned().unwrap_or(Value::Null),
                "persisted_event_id": record.get("persisted_event_id").cloned(),
            }))
        }),
    );

    serve_with(reader, &mut writer, handlers).expect("clean lifecycle");

    let messages = written_messages(&writer);
    assert_eq!(messages.len(), 6);
    assert_eq!(messages[0]["result"]["protocol_version"], PROTOCOL_VERSION);
    assert_eq!(messages[1]["method"], "euler/host/query-provenance");
    assert_eq!(messages[1]["params"]["limit"], 2);
    assert_eq!(messages[2]["method"], "euler/host/write-artifact");
    assert_eq!(messages[2]["params"]["bytes_base64"], "e30=");
    assert_eq!(
        messages[3],
        json!({
            "jsonrpc": "2.0",
            "id": "client-3",
            "method": "euler/host/update-plan-presentation",
            "params": {
                "revision": 2,
                "status": "active",
                "explanation": "Implement the bridge",
                "items": [
                    {"step": "Inspect", "status": "completed"},
                    {"step": "Implement", "status": "in_progress"},
                ],
            },
        })
    );
    assert_eq!(messages[4]["id"], 2);
    assert_eq!(messages[4]["result"]["persisted_event_id"], "a1");
    assert_eq!(messages[5]["id"], 3);
    assert_eq!(messages[5]["result"], json!({}));
}

#[test]
fn unknown_command_reports_method_not_found_and_lifecycle_completes() {
    let [shutdown, exit] = shutdown_and_exit(3);
    let reader = script(&[
        initialize(1),
        initialized(),
        command(2, "nope", Value::Null),
        shutdown,
        exit,
    ]);
    let mut writer: TestWriter = Vec::new();
    let handlers: BTreeMap<String, Handler<TestReader, &mut TestWriter>> = BTreeMap::new();

    serve_with(reader, &mut writer, handlers).expect("clean lifecycle");

    let messages = written_messages(&writer);
    assert_eq!(messages[1]["error"]["code"], -32601);
}

#[test]
fn handler_failure_is_a_generic_error_without_details() {
    let [shutdown, exit] = shutdown_and_exit(3);
    let reader = script(&[
        initialize(1),
        initialized(),
        command(2, "boom", Value::Null),
        shutdown,
        exit,
    ]);
    let mut writer: TestWriter = Vec::new();
    let mut handlers: BTreeMap<String, Handler<TestReader, &mut TestWriter>> = BTreeMap::new();
    handlers.insert(
        "boom".to_owned(),
        Box::new(|_, _| Err(Error::Command("secret detail".to_owned()))),
    );

    serve_with(reader, &mut writer, handlers).expect("clean lifecycle");

    let messages = written_messages(&writer);
    assert_eq!(messages[1]["error"]["code"], -32000);
    assert_eq!(messages[1]["error"]["message"], "extension command failed");
    assert!(!writer
        .windows(b"secret detail".len())
        .any(|window| window == b"secret detail"));
}

#[test]
fn cancellation_during_a_host_request_reports_cancelled() {
    let [shutdown, exit] = shutdown_and_exit(3);
    let reader = script(&[
        initialize(1),
        initialized(),
        command(2, "slow", Value::Null),
        json!({
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {"id": 2},
        }),
        shutdown,
        exit,
    ]);
    let mut writer: TestWriter = Vec::new();
    let mut handlers: BTreeMap<String, Handler<TestReader, &mut TestWriter>> = BTreeMap::new();
    handlers.insert(
        "slow".to_owned(),
        Box::new(|_, host| {
            host.query_provenance(&ProvenanceQuery::default())?;
            unreachable!("cancelled before the host answered");
        }),
    );

    serve_with(reader, &mut writer, handlers).expect("clean lifecycle");

    let messages = written_messages(&writer);
    assert_eq!(messages[2]["error"]["code"], -32800);
}

#[test]
fn malformed_or_wrong_target_cancellations_fail_closed() {
    let malformed_notifications = [
        json!({
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {"id": 999},
        }),
        json!({"jsonrpc": "2.0", "method": "$/cancelRequest"}),
        json!({
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {},
        }),
        json!({
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": [],
        }),
        json!({
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {"id": true},
        }),
        json!({
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {"id": 2.5},
        }),
        json!({
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {"id": 2, "extra": true},
        }),
        json!({
            "jsonrpc": "2.0",
            "id": "client-1",
            "method": "$/cancelRequest",
            "params": {"id": 2},
        }),
    ];

    for notification in malformed_notifications {
        let [shutdown, exit] = shutdown_and_exit(3);
        let reader = script(&[
            initialize(1),
            initialized(),
            command(2, "slow", Value::Null),
            notification,
            shutdown,
            exit,
        ]);
        let mut writer: TestWriter = Vec::new();
        let mut handlers: BTreeMap<String, Handler<TestReader, &mut TestWriter>> = BTreeMap::new();
        handlers.insert(
            "slow".to_owned(),
            Box::new(|_, host| {
                host.query_provenance(&ProvenanceQuery::default())?;
                unreachable!("invalid cancellation cannot complete a host request");
            }),
        );

        serve_with(reader, &mut writer, handlers).expect("sanitized command failure");

        let messages = written_messages(&writer);
        assert_eq!(messages[2]["error"]["code"], -32000);
        assert_eq!(messages[2]["error"]["message"], "extension command failed");
    }
}

#[test]
fn request_ids_are_strings_or_bounded_integers() {
    for id in [json!("request-id"), json!(i64::MIN), json!(u64::MAX)] {
        let message = json!({"jsonrpc": "2.0", "id": id, "method": "initialize"});
        let object = message.as_object().expect("request object");
        assert_eq!(
            require_request(object, "initialize").expect("valid request id"),
            id
        );
    }

    for id in [
        json!(true),
        json!(false),
        json!(null),
        json!(1.0),
        json!(1.5),
        json!({}),
    ] {
        let message = json!({"jsonrpc": "2.0", "id": id, "method": "initialize"});
        let object = message.as_object().expect("request object");
        assert!(require_request(object, "initialize").is_err());
    }
}

#[test]
fn integer_request_ids_outside_the_wire_bounds_are_rejected() {
    for id in ["-9223372036854775809", "18446744073709551616"] {
        let line = format!("{{\"jsonrpc\":\"2.0\",\"id\":{id},\"method\":\"initialize\"}}\n");
        let mut wire = Wire::new(Cursor::new(line.into_bytes()), Vec::new());
        let message = wire.read().expect("well-formed JSON");
        assert!(require_request(&message, "initialize").is_err());
    }
}

#[test]
fn incompatible_protocol_version_is_refused_up_front() {
    let reader = script(&[json!({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocol_versions": ["euler-managed-process/999"]},
    })]);
    let mut writer: TestWriter = Vec::new();
    let handlers: BTreeMap<String, Handler<TestReader, &mut TestWriter>> = BTreeMap::new();

    serve_with(reader, &mut writer, handlers).expect("refusal is a clean return");

    let messages = written_messages(&writer);
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0]["error"]["code"], -32602);
}

#[test]
fn non_object_handler_results_are_rejected() {
    let [shutdown, exit] = shutdown_and_exit(3);
    let reader = script(&[
        initialize(1),
        initialized(),
        command(2, "scalar", Value::Null),
        shutdown,
        exit,
    ]);
    let mut writer: TestWriter = Vec::new();
    let mut handlers: BTreeMap<String, Handler<TestReader, &mut TestWriter>> = BTreeMap::new();
    handlers.insert("scalar".to_owned(), Box::new(|_, _| Ok(json!(42))));

    serve_with(reader, &mut writer, handlers).expect("clean lifecycle");

    let messages = written_messages(&writer);
    assert_eq!(messages[1]["error"]["code"], -32000);
}
