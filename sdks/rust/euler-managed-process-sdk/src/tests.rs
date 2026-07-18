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
            Ok(json!({
                "events": page.get("events").cloned().unwrap_or(Value::Null),
                "persisted_event_id": record.get("persisted_event_id").cloned(),
            }))
        }),
    );

    serve_with(reader, &mut writer, handlers).expect("clean lifecycle");

    let messages = written_messages(&writer);
    assert_eq!(messages.len(), 5);
    assert_eq!(messages[0]["result"]["protocol_version"], PROTOCOL_VERSION);
    assert_eq!(messages[1]["method"], "euler/host/query-provenance");
    assert_eq!(messages[1]["params"]["limit"], 2);
    assert_eq!(messages[2]["method"], "euler/host/write-artifact");
    assert_eq!(messages[2]["params"]["bytes_base64"], "e30=");
    assert_eq!(messages[3]["id"], 2);
    assert_eq!(messages[3]["result"]["persisted_event_id"], "a1");
    assert_eq!(messages[4]["id"], 3);
    assert_eq!(messages[4]["result"], json!({}));
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
        json!({"jsonrpc": "2.0", "method": "$/cancelRequest"}),
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
