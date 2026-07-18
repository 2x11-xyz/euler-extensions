//! A dependency-light JSON-RPC stdio client for Euler process extensions.
//!
//! The protocol is deliberately small and language-neutral: newline-delimited
//! JSON-RPC 2.0 over stdio, one command invocation per process lifetime. This
//! crate is an ergonomic Rust client, not a Rust-specific runtime mode; the
//! wire protocol is the only contract.

use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::io::{BufRead, Write};

pub const PROTOCOL_VERSION: &str = "euler-managed-process/1";
const DEFAULT_MAX_MESSAGE_BYTES: usize = 1024 * 1024;

/// Errors surfaced to extension authors.
#[derive(Debug)]
pub enum Error {
    /// The host or peer sent a message outside the managed-process contract.
    Protocol(String),
    /// A capability-gated host operation did not succeed.
    Host(String),
    /// Euler cancelled the in-flight command before it completed.
    Cancelled,
    /// The command handler failed; serve reports a generic failure to Euler
    /// (implementation details never enter provenance as extension output).
    Command(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Protocol(message) => write!(f, "protocol error: {message}"),
            Error::Host(message) => write!(f, "host error: {message}"),
            Error::Cancelled => write!(f, "cancelled"),
            Error::Command(message) => write!(f, "command error: {message}"),
        }
    }
}

impl std::error::Error for Error {}

fn protocol(message: impl Into<String>) -> Error {
    Error::Protocol(message.into())
}

struct Wire<R, W> {
    reader: R,
    writer: W,
    max_message_bytes: usize,
}

impl<R: BufRead, W: Write> Wire<R, W> {
    fn new(reader: R, writer: W) -> Self {
        Self {
            reader,
            writer,
            max_message_bytes: DEFAULT_MAX_MESSAGE_BYTES,
        }
    }

    fn read(&mut self) -> Result<Map<String, Value>, Error> {
        // The host limits the JSON payload, not its terminating newline. Read
        // up to two bytes beyond the cap so a legal boundary frame and an
        // overlong frame are unambiguous.
        use std::io::Read as _;
        let mut line = Vec::new();
        let mut limited = self.reader.by_ref().take(self.max_message_bytes as u64 + 2);
        limited
            .read_until(b'\n', &mut line)
            .map_err(|error| protocol(error.to_string()))?;
        if line.is_empty() || line.len() > self.max_message_bytes + 1 || line.last() != Some(&b'\n')
        {
            return Err(protocol("invalid protocol framing"));
        }
        let message: Value =
            serde_json::from_slice(&line).map_err(|_| protocol("invalid protocol message"))?;
        let object = match message {
            Value::Object(object) if object.get("jsonrpc") == Some(&json!("2.0")) => object,
            _ => return Err(protocol("invalid protocol message")),
        };
        Ok(object)
    }

    fn write(&mut self, message: &Value) -> Result<(), Error> {
        let encoded = serde_json::to_vec(message).map_err(|error| protocol(error.to_string()))?;
        if encoded.len() > self.max_message_bytes {
            return Err(protocol("protocol message exceeds host limit"));
        }
        self.writer
            .write_all(&encoded)
            .and_then(|()| self.writer.write_all(b"\n"))
            .and_then(|()| self.writer.flush())
            .map_err(|error| protocol(error.to_string()))
    }

    fn set_max_message_bytes(&mut self, maximum: Option<&Value>) {
        if let Some(maximum) = maximum.and_then(Value::as_u64) {
            let maximum = usize::try_from(maximum).unwrap_or(0);
            if maximum > 0 && maximum <= DEFAULT_MAX_MESSAGE_BYTES {
                self.max_message_bytes = maximum;
            }
        }
    }
}

/// Options for [`Host::query_provenance`], mirroring the host defaults.
#[derive(Clone, Debug)]
pub struct ProvenanceQuery {
    pub after_event_id: Option<String>,
    pub kinds: Vec<String>,
    pub limit: u64,
    pub scan_limit: u64,
    pub include_blob_fields: bool,
    pub blob_byte_limit: u64,
}

impl Default for ProvenanceQuery {
    fn default() -> Self {
        Self {
            after_event_id: None,
            kinds: Vec::new(),
            limit: 128,
            scan_limit: 1024,
            include_blob_fields: false,
            blob_byte_limit: 1024 * 1024,
        }
    }
}

/// An artifact to persist through the host.
#[derive(Clone, Debug)]
pub struct ArtifactWrite {
    pub display_name: String,
    pub media_type: String,
    pub bytes: Vec<u8>,
    pub source_event_ids: Vec<String>,
    pub metadata: Map<String, Value>,
}

/// Capability-gated host APIs available during a command invocation.
pub struct Host<'wire, R, W> {
    wire: &'wire mut Wire<R, W>,
    next_request_id: u64,
}

impl<R: BufRead, W: Write> Host<'_, R, W> {
    /// Report user-visible progress. `fraction`, when given, must be within
    /// `0..=1`; the message must be 1..=4096 UTF-8 bytes.
    pub fn progress(&mut self, message: &str, fraction: Option<f64>) -> Result<(), Error> {
        if message.is_empty() || message.len() > 4096 {
            return Err(Error::Command(
                "progress message must be 1..4096 UTF-8 bytes".to_owned(),
            ));
        }
        let mut params = Map::from_iter([("message".to_owned(), json!(message))]);
        if let Some(fraction) = fraction {
            if !fraction.is_finite() || !(0.0..=1.0).contains(&fraction) {
                return Err(Error::Command(
                    "progress fraction must be between 0 and 1".to_owned(),
                ));
            }
            params.insert("fraction".to_owned(), json!(fraction));
        }
        self.wire.write(&json!({
            "jsonrpc": "2.0",
            "method": "euler/progress",
            "params": params,
        }))
    }

    pub fn query_provenance(&mut self, query: &ProvenanceQuery) -> Result<Value, Error> {
        self.request(
            "euler/host/query-provenance",
            json!({
                "after_event_id": query.after_event_id,
                "kinds": query.kinds,
                "limit": query.limit,
                "scan_limit": query.scan_limit,
                "include_blob_fields": query.include_blob_fields,
                "blob_byte_limit": query.blob_byte_limit,
            }),
        )
    }

    pub fn read_diagnostics(&mut self, tail_lines: u64, max_bytes: u64) -> Result<Value, Error> {
        self.request(
            "euler/host/read-diagnostics",
            json!({"tail_lines": tail_lines, "max_bytes": max_bytes}),
        )
    }

    pub fn state_dir(&mut self) -> Result<String, Error> {
        let result = self.request("euler/host/state-dir", json!({}))?;
        result
            .get("path")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| protocol("host returned an invalid state directory"))
    }

    pub fn write_artifact(&mut self, artifact: &ArtifactWrite) -> Result<Value, Error> {
        use base64::Engine as _;
        self.request(
            "euler/host/write-artifact",
            json!({
                "display_name": artifact.display_name,
                "media_type": artifact.media_type,
                "bytes_base64":
                    base64::engine::general_purpose::STANDARD.encode(&artifact.bytes),
                "source_event_ids": artifact.source_event_ids,
                "metadata": artifact.metadata,
            }),
        )
    }

    pub fn load_checkpoint(&mut self, name: &str) -> Result<Value, Error> {
        self.request("euler/host/load-checkpoint", json!({"name": name}))
    }

    pub fn store_checkpoint(&mut self, name: &str, checkpoint: &Value) -> Result<(), Error> {
        self.request(
            "euler/host/store-checkpoint",
            json!({"name": name, "checkpoint": checkpoint}),
        )
        .map(|_| ())
    }

    pub fn record_agent_task_result(
        &mut self,
        task: &Value,
        result: &Value,
    ) -> Result<Value, Error> {
        self.request(
            "euler/host/record-agent-task-result",
            json!({"task": task, "result": result}),
        )
    }

    pub fn update_context_slot(&mut self, slot: &str, content: &str) -> Result<(), Error> {
        self.request(
            "euler/host/update-context-slot",
            json!({"slot": slot, "content": content}),
        )
        .map(|_| ())
    }

    pub fn spawn_agent(&mut self, task: &Value) -> Result<Value, Error> {
        self.request("euler/host/spawn-agent", task.clone())
    }

    pub fn spawn_agents(&mut self, tasks: &[Value]) -> Result<Vec<Value>, Error> {
        let result = self.request("euler/host/spawn-agents", json!({"tasks": tasks}))?;
        match result {
            Value::Array(outcomes) => Ok(outcomes),
            _ => Err(protocol("host returned invalid agent outcomes")),
        }
    }

    fn request(&mut self, method: &str, params: Value) -> Result<Value, Error> {
        let request_id = format!("client-{}", self.next_request_id);
        self.next_request_id += 1;
        self.wire.write(&json!({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }))?;
        let message = self.wire.read()?;
        if message.get("id").and_then(Value::as_str) == Some(request_id.as_str()) {
            if let Some(result) = message.get("result") {
                if !message.contains_key("error") {
                    return Ok(result.clone());
                }
            }
            let error = message
                .get("error")
                .and_then(Value::as_object)
                .and_then(|error| error.get("message"))
                .and_then(Value::as_str)
                .unwrap_or("host operation failed");
            return Err(Error::Host(error.to_owned()));
        }
        if message.get("method").and_then(Value::as_str) == Some("$/cancelRequest") {
            return Err(Error::Cancelled);
        }
        Err(protocol(
            "unexpected message while waiting for host response",
        ))
    }
}

/// The JSON input supplied to one declared extension command.
pub struct CommandContext {
    pub command: String,
    pub input: Value,
}

/// A declared command handler. It must return a JSON object; errors are
/// reported to Euler as a generic failure (stderr and implementation details
/// never enter the model canvas or provenance as extension output).
pub type Handler<R, W> = Box<dyn Fn(&CommandContext, &mut Host<'_, R, W>) -> Result<Value, Error>>;

/// Run declared command handlers until Euler sends the clean exit signal,
/// speaking the protocol over stdio.
pub fn serve(handlers: BTreeMap<String, Handler<std::io::StdinLock<'static>, std::io::Stdout>>) {
    let stdin = std::io::stdin().lock();
    let stdout = std::io::stdout();
    // Protocol violations are unrecoverable by design: the host owns the
    // lifecycle, so a broken frame means exit, mirroring the reference
    // client.
    let _ = serve_with(stdin, stdout, handlers);
}

/// The transport-generic core of [`serve`], separated so tests can drive the
/// full lifecycle in memory.
pub fn serve_with<R: BufRead, W: Write>(
    reader: R,
    writer: W,
    handlers: BTreeMap<String, Handler<R, W>>,
) -> Result<(), Error> {
    let mut wire = Wire::new(reader, writer);

    let initialize = wire.read()?;
    let initialize_id = require_request(&initialize, "initialize")?;
    let compatible = initialize
        .get("params")
        .and_then(Value::as_object)
        .and_then(|params| params.get("protocol_versions"))
        .and_then(Value::as_array)
        .is_some_and(|versions| versions.iter().any(|value| value == PROTOCOL_VERSION));
    if !compatible {
        write_error(
            &mut wire,
            &initialize_id,
            -32602,
            "no compatible protocol version",
        )?;
        return Ok(());
    }
    let limits = initialize
        .get("params")
        .and_then(Value::as_object)
        .and_then(|params| params.get("limits"))
        .and_then(Value::as_object)
        .and_then(|limits| limits.get("max_message_bytes"))
        .cloned();
    wire.set_max_message_bytes(limits.as_ref());
    write_result(
        &mut wire,
        &initialize_id,
        json!({"protocol_version": PROTOCOL_VERSION}),
    )?;

    let initialized = wire.read()?;
    if initialized.get("method").and_then(Value::as_str) != Some("initialized")
        || initialized.contains_key("id")
    {
        return Err(protocol("expected initialized notification"));
    }

    let command = wire.read()?;
    let command_id = require_request(&command, "euler/command")?;
    let params = command.get("params").and_then(Value::as_object);
    match params
        .and_then(|params| params.get("command"))
        .and_then(Value::as_str)
    {
        None => write_error(&mut wire, &command_id, -32602, "invalid command request")?,
        Some(name) => match handlers.get(name) {
            None => write_error(&mut wire, &command_id, -32601, "unknown extension command")?,
            Some(handler) => {
                let context = CommandContext {
                    command: name.to_owned(),
                    input: params
                        .and_then(|params| params.get("input"))
                        .cloned()
                        .unwrap_or(Value::Null),
                };
                let name = context.command.clone();
                let outcome = {
                    let mut host = Host {
                        wire: &mut wire,
                        next_request_id: 1,
                    };
                    handler(&context, &mut host)
                };
                let _ = name;
                match outcome {
                    Ok(Value::Object(result)) => {
                        write_result(&mut wire, &command_id, Value::Object(result))?;
                    }
                    Ok(_) => {
                        write_error(&mut wire, &command_id, -32000, "extension command failed")?;
                    }
                    Err(Error::Cancelled) => {
                        write_error(
                            &mut wire,
                            &command_id,
                            -32800,
                            "extension command cancelled",
                        )?;
                    }
                    Err(_) => {
                        write_error(&mut wire, &command_id, -32000, "extension command failed")?;
                    }
                }
            }
        },
    }

    let shutdown = wire.read()?;
    let shutdown_id = require_request(&shutdown, "shutdown")?;
    write_result(&mut wire, &shutdown_id, json!({}))?;
    let exit = wire.read()?;
    if exit.get("method").and_then(Value::as_str) != Some("exit") || exit.contains_key("id") {
        return Err(protocol("expected exit notification"));
    }
    Ok(())
}

fn require_request(message: &Map<String, Value>, method: &str) -> Result<Value, Error> {
    let id = message.get("id");
    let id_valid = matches!(id, Some(Value::String(_)) | Some(Value::Number(_)));
    if message.get("method").and_then(Value::as_str) != Some(method) || !id_valid {
        return Err(protocol(format!("expected {method} request")));
    }
    Ok(id.cloned().expect("id checked above"))
}

fn write_result<R: BufRead, W: Write>(
    wire: &mut Wire<R, W>,
    id: &Value,
    result: Value,
) -> Result<(), Error> {
    wire.write(&json!({"jsonrpc": "2.0", "id": id, "result": result}))
}

fn write_error<R: BufRead, W: Write>(
    wire: &mut Wire<R, W>,
    id: &Value,
    code: i64,
    message: &str,
) -> Result<(), Error> {
    wire.write(&json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message},
    }))
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
