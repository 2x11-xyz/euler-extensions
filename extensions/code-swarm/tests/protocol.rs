//! Protocol-level end-to-end test: spawn the built `code-swarm` binary as a
//! subprocess and drive the managed-process wire as the host would, answering
//! its `euler/host/spawn-agents` and `euler/host/write-artifact` requests with
//! host-shaped JSON and asserting the consolidated command result.
//!
//! `review` is agent-only, so this is the honest verification path: a user
//! surface would refuse `extension_run`, but the wire contract is what euler
//! wires the managed extension against at cutover.

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

const PROTOCOL_VERSION: &str = "euler-managed-process/1";

struct Session {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Session {
    fn spawn() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_code-swarm"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn code-swarm binary");
        let stdin = child.stdin.take().expect("child stdin");
        let stdout = BufReader::new(child.stdout.take().expect("child stdout"));
        Self {
            child,
            stdin,
            stdout,
        }
    }

    fn send(&mut self, message: &Value) {
        let mut line = serde_json::to_vec(message).expect("encode message");
        line.push(b'\n');
        self.stdin.write_all(&line).expect("write to child");
        self.stdin.flush().expect("flush child stdin");
    }

    fn recv(&mut self) -> Value {
        let mut line = String::new();
        let read = self.stdout.read_line(&mut line).expect("read from child");
        assert!(read > 0, "child closed stdout unexpectedly");
        serde_json::from_str(line.trim_end()).expect("valid JSON from child")
    }

    fn initialize(&mut self) {
        self.send(&json!({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocol_versions": [PROTOCOL_VERSION]},
        }));
        let ack = self.recv();
        assert_eq!(ack["id"], json!(1));
        assert_eq!(ack["result"]["protocol_version"], PROTOCOL_VERSION);
        self.send(&json!({"jsonrpc": "2.0", "method": "initialized"}));
    }

    fn respond(&mut self, request: &Value, result: Value) {
        let id = request["id"].clone();
        assert!(!id.is_null(), "host request must carry an id");
        self.send(&json!({"jsonrpc": "2.0", "id": id, "result": result}));
    }

    fn shutdown_and_exit(&mut self) {
        self.send(&json!({"jsonrpc": "2.0", "id": 3, "method": "shutdown"}));
        let ack = self.recv();
        assert_eq!(ack["id"], json!(3));
        assert_eq!(ack["result"], json!({}));
        self.send(&json!({"jsonrpc": "2.0", "method": "exit"}));
        let status = self.child.wait().expect("child exits cleanly");
        assert!(status.success(), "child exit status: {status:?}");
    }
}

fn outcome(index: usize, provider: &str, model: &str, ok: bool) -> Value {
    json!({
        "ok": ok,
        "summary": if ok { "reviewed" } else { "reviewer failed" },
        "output": format!("finding number {index}"),
        "error": if ok { Value::Null } else { json!("budget exhausted") },
        "provider": provider,
        "model": model,
        "child_agent_id": format!("child_{index}"),
        "spawn_event_id": format!("spawn_{index}"),
        "result_event_id": format!("result_{index}"),
    })
}

#[test]
fn review_consolidates_a_two_reviewer_batch_over_the_wire() {
    let mut session = Session::spawn();
    session.initialize();

    session.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "euler/command",
        "params": {
            "command": "review",
            "input": {
                "models": ["anthropic::claude-opus-5", "openai::gpt-5.5"],
                "prompt": "focus on the parser boundary",
                "context": "the small explicit diff under review",
            },
        },
    }));

    // First host request: one concurrent spawn-agents batch of two tasks.
    let spawn = session.recv();
    assert_eq!(spawn["method"], "euler/host/spawn-agents");
    let tasks = spawn["params"]["tasks"]
        .as_array()
        .expect("tasks array in spawn-agents request");
    assert_eq!(tasks.len(), 2, "model selection is the agent count");
    assert_eq!(tasks[0]["persona"], "code-swarm-correctness");
    assert_eq!(tasks[0]["provider"], "anthropic");
    assert_eq!(tasks[0]["model"], "claude-opus-5");
    assert_eq!(tasks[1]["persona"], "code-swarm-safety");
    assert_eq!(tasks[0]["include_parent_canvas"], json!(false));
    assert_eq!(tasks[0]["capabilities"], json!([]));
    assert_eq!(tasks[0]["max_turns"], json!(1));
    assert_eq!(tasks[0]["max_tool_calls"], json!(0));
    assert_eq!(
        tasks[0]["explicit_context"],
        json!("the small explicit diff under review")
    );

    session.respond(
        &spawn,
        json!([
            outcome(1, "anthropic", "claude-opus-5", true),
            outcome(2, "openai", "gpt-5.5", false),
        ]),
    );

    // Second host request: the consolidated artifact write.
    let write = session.recv();
    assert_eq!(write["method"], "euler/host/write-artifact");
    assert_eq!(
        write["params"]["media_type"],
        "application/vnd.euler.code-swarm.review.v1+json"
    );
    assert_eq!(
        write["params"]["source_event_ids"],
        json!(["result_1", "result_2"])
    );
    assert_eq!(write["params"]["metadata"]["reviewer_count"], json!(2));

    session.respond(
        &write,
        json!({
            "persisted_event_id": "event-artifact",
            "relative_path": "extensions/code-swarm/artifacts/hash",
            "sha256": "hash",
            "byte_len": 128,
        }),
    );

    // Command result: consolidation shape, matching the bundled crate.
    let result = session.recv();
    assert_eq!(result["id"], json!(2));
    let payload = &result["result"];
    assert_eq!(payload["persisted_event_id"], "event-artifact");
    assert_eq!(
        payload["relative_path"],
        "extensions/code-swarm/artifacts/hash"
    );
    assert_eq!(payload["sha256"], "hash");
    assert_eq!(payload["byte_len"], json!(128));
    assert_eq!(payload["reviewer_count"], json!(2));
    assert_eq!(payload["succeeded"], json!(1));
    assert_eq!(payload["failed"], json!(1));
    assert_eq!(payload["reviewers"][0]["persona"], "code-swarm-correctness");
    assert_eq!(payload["reviewers"][0]["ok"], json!(true));
    assert_eq!(payload["reviewers"][0]["findings"], "finding number 1");
    assert_eq!(payload["reviewers"][1]["ok"], json!(false));
    assert_eq!(payload["reviewers"][1]["error"], "budget exhausted");

    session.shutdown_and_exit();
}

#[test]
fn unconfigured_models_fail_without_spawning_anything() {
    let mut session = Session::spawn();
    session.initialize();

    session.send(&json!({
        "jsonrpc": "2.0", "id": 2, "method": "euler/command",
        "params": {
            "command": "review",
            "input": {"models": [], "prompt": "p", "context": "c"},
        },
    }));

    // No spawn-agents request precedes the failure: the next message is the
    // command error. The specific unconfigured message stays out of the wire by
    // design (implementation detail never enters provenance); the host surfaces
    // a generic command failure.
    let result = session.recv();
    assert_eq!(result["id"], json!(2));
    assert_eq!(result["error"]["code"], json!(-32000));
    assert_eq!(result["error"]["message"], "extension command failed");
    assert!(result.get("result").is_none());

    session.shutdown_and_exit();
}
