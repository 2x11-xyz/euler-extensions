//! Protocol-level end-to-end tests: spawn the built `code-swarm` binary as a
//! subprocess and drive the managed-process wire as the host would, answering
//! its `euler/host/read-extension-config`, `euler/host/spawn-agents`,
//! `euler/host/write-artifact`, and `euler/host/write-extension-config`
//! requests with host-shaped JSON and asserting the command results.
//!
//! `review` is agent-only (the `code_swarm_review` model tool), so this is the
//! honest verification path for it: a user surface would refuse
//! `extension_run`, but the wire contract is what Euler drives.

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

    fn respond_error(&mut self, request: &Value) {
        let id = request["id"].clone();
        assert!(!id.is_null(), "host request must carry an id");
        self.send(&json!({
            "jsonrpc": "2.0", "id": id,
            "error": {"code": -32000, "message": "host operation failed"},
        }));
    }

    fn command(&mut self, command: &str, input: Value) {
        self.send(&json!({
            "jsonrpc": "2.0", "id": 2, "method": "euler/command",
            "params": {"command": command, "input": input},
        }));
    }

    /// The command's final result payload.
    fn command_result(&mut self) -> Value {
        let result = self.recv();
        assert_eq!(
            result["id"],
            json!(2),
            "expected the command result: {result}"
        );
        assert!(result.get("error").is_none(), "command failed: {result}");
        result["result"].clone()
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

    session.command(
        "review",
        json!({
            "models": ["anthropic::claude-opus-5", "openai::gpt-5.5"],
            "focus": "focus on the parser boundary",
            "context": "the small explicit diff under review",
        }),
    );

    // Explicit models win outright: no config read precedes the one
    // concurrent spawn-agents batch of two tasks.
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

    // Command result: consolidation shape, with partial failure rendered
    // per reviewer rather than failing the command.
    let payload = session.command_result();
    assert_eq!(payload["review_ran"], json!(true));
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

/// Run a review without explicit models, answer the config read with
/// `documents`, and return the spawn-agents tasks.
fn configured_review_tasks(documents: Value) -> Vec<Value> {
    let mut session = Session::spawn();
    session.initialize();
    session.command(
        "review",
        json!({"focus": "question", "context": "material"}),
    );

    let read = session.recv();
    assert_eq!(read["method"], "euler/host/read-extension-config");
    assert_eq!(read["params"], json!({}));
    session.respond(&read, documents);

    let spawn = session.recv();
    assert_eq!(spawn["method"], "euler/host/spawn-agents");
    let tasks = spawn["params"]["tasks"].as_array().cloned().expect("tasks");
    let outcomes = tasks
        .iter()
        .enumerate()
        .map(|(index, task)| {
            outcome(
                index + 1,
                task["provider"].as_str().unwrap(),
                task["model"].as_str().unwrap(),
                true,
            )
        })
        .collect::<Vec<_>>();
    session.respond(&spawn, Value::Array(outcomes));
    let write = session.recv();
    assert_eq!(write["method"], "euler/host/write-artifact");
    session.respond(
        &write,
        json!({"persisted_event_id": "a", "relative_path": "p", "sha256": "h", "byte_len": 1}),
    );
    assert_eq!(session.command_result()["review_ran"], json!(true));
    session.shutdown_and_exit();
    tasks
}

#[test]
fn review_resolves_the_project_config_before_the_user_config() {
    let tasks = configured_review_tasks(json!({
        "project": {
            "version": 1,
            "reviewers": [
                {"target": "anthropic::claude-opus-5", "persona": "tests"},
                {"target": "openai::gpt-5.5", "persona": "safety"},
            ],
            "max_tokens": 1000,
        },
        "user": {"version": 1, "reviewers": [{"target": "user::model"}]},
    }));
    assert_eq!(tasks.len(), 2);
    assert_eq!(tasks[0]["provider"], "anthropic");
    assert_eq!(tasks[0]["persona"], "code-swarm-tests");
    assert_eq!(tasks[0]["max_tokens"], json!(1000));
    assert_eq!(tasks[1]["model"], "gpt-5.5");
    assert_eq!(tasks[1]["persona"], "code-swarm-safety");
}

#[test]
fn review_falls_back_to_the_user_config() {
    let tasks = configured_review_tasks(json!({
        "project": null,
        "user": {"version": 1, "reviewers": [{"target": "user::model"}]},
    }));
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0]["provider"], "user");
    assert_eq!(tasks[0]["model"], "model");
    assert_eq!(tasks[0]["persona"], "code-swarm-correctness");
    assert_eq!(tasks[0]["max_tokens"], json!(8192));
}

/// Run a review whose config read gets `answer`, and return the not-run
/// result. Nothing spawns and no artifact is written.
fn not_run_review(answer: impl FnOnce(&mut Session, &Value)) -> Value {
    let mut session = Session::spawn();
    session.initialize();
    session.command(
        "review",
        json!({"focus": "question", "context": "material"}),
    );
    let read = session.recv();
    assert_eq!(read["method"], "euler/host/read-extension-config");
    answer(&mut session, &read);
    // The next message is the command result, not a spawn request.
    let payload = session.command_result();
    session.shutdown_and_exit();
    assert_eq!(payload["review_ran"], json!(false), "{payload}");
    payload
}

#[test]
fn unconfigured_review_returns_an_honest_not_run_result_without_spawning() {
    let payload = not_run_review(|session, read| {
        session.respond(read, json!({"project": null, "user": null}));
    });
    assert_eq!(payload["reason"], "unconfigured");
    let message = payload["message"].as_str().expect("message");
    for fragment in [
        "do not guess providers or models",
        "code-swarm.configure",
        "code-swarm.configure-user",
        "euler extension run code-swarm.import <session> --input-file .euler/code-swarm.json",
        "/code-swarm.import",
    ] {
        assert!(
            message.contains(fragment),
            "missing `{fragment}`: {message}"
        );
    }
}

#[test]
fn invalid_config_returns_a_not_run_result_and_never_falls_through() {
    let payload = not_run_review(|session, read| {
        session.respond(
            read,
            json!({
                "project": {"version": 1, "reviewers": []},
                "user": {"version": 1, "reviewers": [{"target": "user::model"}]},
            }),
        );
    });
    assert_eq!(payload["reason"], "invalid_config");
    assert!(payload["message"]
        .as_str()
        .unwrap()
        .contains("the project code-swarm config is invalid"));

    // A document the host refuses (malformed, oversized, symlinked).
    let payload = not_run_review(|session, read| session.respond_error(read));
    assert_eq!(payload["reason"], "invalid_config");
}

#[test]
fn failed_reviewer_batch_returns_a_not_run_result_without_writing() {
    let mut session = Session::spawn();
    session.initialize();
    session.command(
        "review",
        json!({"models": ["unauthenticated::model"], "focus": "question", "context": "material"}),
    );
    let spawn = session.recv();
    assert_eq!(spawn["method"], "euler/host/spawn-agents");
    session.respond_error(&spawn);
    // The next message is the command result, not an artifact write.
    let payload = session.command_result();
    session.shutdown_and_exit();
    assert_eq!(payload["review_ran"], json!(false), "{payload}");
    assert_eq!(payload["reason"], "reviewer_batch_failed");
    let message = payload["message"].as_str().expect("message");
    for fragment in [
        "do not guess providers or models",
        "/login <provider>",
        "code-swarm.configure",
    ] {
        assert!(
            message.contains(fragment),
            "missing `{fragment}`: {message}"
        );
    }
}

#[test]
fn cancelled_or_malformed_reviewer_batch_ends_the_command() {
    // Cancellation while the batch runs is a cancelled command, not a not-run
    // result.
    let mut session = Session::spawn();
    session.initialize();
    session.command(
        "review",
        json!({"models": ["a::b"], "focus": "question", "context": "material"}),
    );
    let spawn = session.recv();
    assert_eq!(spawn["method"], "euler/host/spawn-agents");
    session.send(&json!({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 2}}));
    let result = session.recv();
    assert_eq!(result["id"], json!(2));
    assert_eq!(result["error"]["code"], json!(-32800), "{result}");
    session.shutdown_and_exit();

    // A malformed host answer is a protocol failure, not a not-run result.
    let mut session = Session::spawn();
    session.initialize();
    session.command(
        "review",
        json!({"models": ["a::b"], "focus": "question", "context": "material"}),
    );
    let spawn = session.recv();
    session.respond(&spawn, json!("not outcomes"));
    let result = session.recv();
    assert_eq!(result["id"], json!(2));
    assert_eq!(result["error"]["code"], json!(-32000), "{result}");
    session.shutdown_and_exit();
}

#[test]
fn oversized_focus_returns_a_not_run_result() {
    let mut session = Session::spawn();
    session.initialize();
    // Within the schema's 7168-character bound, over the 7168-byte bound.
    session.command(
        "review",
        json!({"models": ["a::b"], "focus": "\u{e9}".repeat(3600), "context": "c"}),
    );
    let payload = session.command_result();
    session.shutdown_and_exit();
    assert_eq!(payload["review_ran"], json!(false));
    assert_eq!(payload["reason"], "input_too_large");
}

/// Run a config command, answer its write, and return the written request
/// params and the command result.
fn config_command(command: &str, input: Value) -> (Value, Value) {
    let mut session = Session::spawn();
    session.initialize();
    session.command(command, input);
    let write = session.recv();
    assert_eq!(write["method"], "euler/host/write-extension-config");
    session.respond(&write, json!({}));
    let payload = session.command_result();
    session.shutdown_and_exit();
    (write["params"].clone(), payload)
}

#[test]
fn configure_writes_the_project_tier_and_configure_user_the_user_tier() {
    for (command, tier) in [("configure", "project"), ("configure-user", "user")] {
        let (params, payload) = config_command(
            command,
            json!({"models": ["anthropic::claude-opus-5", "openai::gpt-5.5"]}),
        );
        assert_eq!(
            params,
            json!({
                "tier": tier,
                "value": {
                    "version": 1,
                    "reviewers": [
                        {"target": "anthropic::claude-opus-5"},
                        {"target": "openai::gpt-5.5"},
                    ],
                },
            })
        );
        assert_eq!(
            payload,
            json!({
                "written": true,
                "tier": tier,
                "reviewers": ["anthropic::claude-opus-5", "openai::gpt-5.5"],
            })
        );
    }
}

#[test]
fn import_writes_a_legacy_document_to_the_matching_tier() {
    let legacy = json!({
        "version": 1,
        "reviewers": [
            {"target": "anthropic::claude-opus-5", "persona": "correctness"},
            {"target": "openai::gpt-5.5", "persona": "tests"},
        ],
        "max_tokens": 4096,
    });
    for (command, tier) in [("import", "project"), ("import-user", "user")] {
        let (params, payload) = config_command(command, legacy.clone());
        assert_eq!(params, json!({"tier": tier, "value": legacy}));
        assert_eq!(payload["written"], json!(true));
        assert_eq!(payload["tier"], tier);
    }
}

#[test]
fn import_refuses_an_invalid_legacy_document_without_writing() {
    let mut session = Session::spawn();
    session.initialize();
    session.command(
        "import",
        json!({"version": 1, "reviewers": [{"target": "a::b", "persona": "tests"}, {"target": "c::d"}]}),
    );
    // No write request precedes the result.
    let payload = session.command_result();
    session.shutdown_and_exit();
    assert_eq!(payload["written"], json!(false));
    assert_eq!(payload["reason"], "invalid_legacy_config");
    assert!(payload["message"]
        .as_str()
        .unwrap()
        .contains("personas must label every reviewer or none"));
}

#[test]
fn manifest_declares_the_review_tool_and_config_commands() {
    let manifest: Value =
        serde_json::from_str(include_str!("../Euler.extension.json")).expect("valid manifest JSON");
    assert_eq!(
        manifest["capabilities"],
        json!(["agent-spawn", "artifact-write", "config-write"])
    );
    let commands = manifest["commands"].as_array().expect("commands");
    let command = |name: &str| {
        commands
            .iter()
            .find(|command| command["name"] == name)
            .unwrap_or_else(|| panic!("missing command {name}"))
            .clone()
    };

    let review = command("review");
    assert_eq!(review["invocation"], "agent-only");
    let tool = &review["model_tool"];
    assert_eq!(tool["name"], "code_swarm_review");
    // The host cap (ADR 0022). Escape-heavy material can still serialize past
    // it; the host then rejects the call with its own input-size error.
    assert_eq!(tool["max_input_bytes"], json!(320 * 1024));
    let properties = &tool["input_schema"]["properties"];
    assert_eq!(properties["focus"]["maxLength"], json!(7 * 1024));
    assert_eq!(properties["context"]["maxLength"], json!(256 * 1024));
    // An empty list is accepted and means "no override" (config resolves).
    assert!(properties["models"].get("minItems").is_none());
    assert_eq!(properties["models"]["maxItems"], json!(5));
    assert_eq!(properties["personas"]["maxItems"], json!(5));
    assert_eq!(properties["max_tokens"]["minimum"], json!(1));
    assert_eq!(
        tool["input_schema"]["required"],
        json!(["focus", "context"])
    );

    for name in ["configure", "configure-user"] {
        let configure = command(name);
        assert_eq!(configure["invocation"], "user");
        assert_eq!(configure["required_capabilities"], json!(["config-write"]));
        assert_eq!(
            configure["args"],
            json!([{"flag": "models", "kind": "model-targets", "max": 5, "required": true}])
        );
    }
    for name in ["import", "import-user"] {
        let import = command(name);
        assert_eq!(import["invocation"], "user");
        assert_eq!(import["required_capabilities"], json!(["config-write"]));
        assert!(import.get("args").is_none());
    }
}
