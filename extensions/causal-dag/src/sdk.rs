//! Local mirror of the `euler-sdk` extension surface, backed by the
//! managed-process wire.
//!
//! The bundled in-process crate was written against `euler_sdk`'s typed
//! `HostApi`, `ProvenancePage`, `ArtifactRecord`, capability, and command
//! descriptor types. This module re-declares that surface verbatim so the
//! ported modules keep their signatures unchanged, and adds [`WireHost`]: a
//! thin adapter that implements [`HostApi`] over an
//! [`euler_managed_process_sdk::Host`]. Every host method maps to exactly one
//! managed-process wire request (see `runtime.rs` dispatch); no capability is
//! faked.
//!
//! Two `euler-agents` constants the observer briefs bound against are inlined
//! here rather than pulling the crate: they mirror `euler_agents`'s
//! `MAX_TASK_BYTES` and `MAX_SYSTEM_PROMPT_BYTES` (both `8 * 1024`). The
//! `AgentTask`/`AgentSystemPrompt` bounds these feed are enforced by the host
//! when the resulting brief is run, so the values must not drift.

use crate::event::{EventEnvelope, JsonObject};
use euler_managed_process_sdk as mp;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::cell::RefCell;
use std::fmt;
use std::io::{BufRead, Write};
use std::path::PathBuf;

/// Mirrors `euler_agents::MAX_TASK_BYTES` (8 * 1024).
pub const MAX_TASK_BYTES: usize = 8 * 1024;
/// Mirrors `euler_agents::MAX_SYSTEM_PROMPT_BYTES` (8 * 1024).
pub const MAX_SYSTEM_PROMPT_BYTES: usize = 8 * 1024;

pub const MAX_CONTEXT_SLOTS_PER_SESSION: usize = 8;

#[derive(
    Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, serde::Deserialize, serde::Serialize,
)]
#[serde(rename_all = "kebab-case")]
pub enum Capability {
    FsRead,
    FsWrite,
    ProvenanceRead,
    DiagnosticsRead,
    ArtifactWrite,
    AgentRecord,
    AgentSpawn,
    ShellExec,
    Network,
    ConfigWrite,
    SecretResolve,
    ContextSlot,
}

impl Capability {
    pub const ALL: &'static [Self] = &[
        Self::FsRead,
        Self::FsWrite,
        Self::ProvenanceRead,
        Self::DiagnosticsRead,
        Self::ArtifactWrite,
        Self::AgentRecord,
        Self::AgentSpawn,
        Self::ShellExec,
        Self::Network,
        Self::ConfigWrite,
        Self::SecretResolve,
        Self::ContextSlot,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::FsRead => "fs-read",
            Self::FsWrite => "fs-write",
            Self::ProvenanceRead => "provenance-read",
            Self::DiagnosticsRead => "diagnostics-read",
            Self::ArtifactWrite => "artifact-write",
            Self::AgentRecord => "agent-record",
            Self::AgentSpawn => "agent-spawn",
            Self::ShellExec => "shell-exec",
            Self::Network => "network",
            Self::ConfigWrite => "config-write",
            Self::SecretResolve => "secret-resolve",
            Self::ContextSlot => "context-slot",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "fs-read" => Some(Self::FsRead),
            "fs-write" => Some(Self::FsWrite),
            "provenance-read" => Some(Self::ProvenanceRead),
            "diagnostics-read" => Some(Self::DiagnosticsRead),
            "artifact-write" => Some(Self::ArtifactWrite),
            "agent-record" => Some(Self::AgentRecord),
            "agent-spawn" => Some(Self::AgentSpawn),
            "shell-exec" => Some(Self::ShellExec),
            "network" => Some(Self::Network),
            "config-write" => Some(Self::ConfigWrite),
            "secret-resolve" => Some(Self::SecretResolve),
            "context-slot" => Some(Self::ContextSlot),
            _ => None,
        }
    }
}

impl fmt::Display for Capability {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExtensionManifest {
    pub id: String,
    pub version: String,
    pub display_name: String,
    pub capabilities: Vec<Capability>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct CommandContext {
    pub input: Value,
}

/// Who may invoke a command directly. A product boundary, not a security one.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Invocation {
    #[default]
    User,
    AgentOnly,
}

impl Invocation {
    pub const fn is_agent_only(self) -> bool {
        matches!(self, Self::AgentOnly)
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::User => "user",
            Self::AgentOnly => "agent-only",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "user" => Some(Self::User),
            "agent-only" => Some(Self::AgentOnly),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommandDescriptor {
    pub name: String,
    pub display_name: String,
    pub summary: String,
    pub required_capabilities: Vec<Capability>,
    pub args: Vec<ArgSpec>,
    pub accepts_session_id: bool,
    pub invocation: Invocation,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArgSpec {
    pub flag: String,
    pub input_key: String,
    pub value_kind: ArgValueKind,
    pub required: bool,
    pub repeatable: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ArgValueKind {
    PositiveInt {
        max: Option<usize>,
    },
    BoundedString {
        max_bytes: usize,
    },
    StringList,
    JsonObjectFile {
        max_bytes: usize,
        reject_wrapper_key: Option<String>,
    },
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProvenanceQuery {
    pub after_event_id: Option<String>,
    pub kinds: Vec<String>,
    pub limit: usize,
    pub scan_limit: usize,
    pub include_blob_fields: bool,
    pub blob_byte_limit: usize,
}

impl ProvenanceQuery {
    pub fn new(limit: usize) -> Self {
        Self {
            after_event_id: None,
            kinds: Vec::new(),
            limit,
            scan_limit: 1024,
            include_blob_fields: false,
            blob_byte_limit: 1024 * 1024,
        }
    }
}

#[derive(Clone, Debug, PartialEq, serde::Deserialize, serde::Serialize)]
pub struct ProvenancePage {
    pub events: Vec<EventEnvelope>,
    pub applied_limit: usize,
    pub applied_scan_limit: usize,
    pub scanned_events: usize,
    pub watermark_event_id: Option<String>,
    pub next_after_event_id: Option<String>,
    pub truncated: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct DiagnosticsQuery {
    pub tail_lines: usize,
    pub max_bytes: usize,
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
pub struct DiagnosticsPage {
    pub lines: Vec<String>,
    pub truncated: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct SpawnAgentTask {
    pub task: String,
    pub persona: String,
    pub provider: String,
    pub model: String,
    pub system_prompt: String,
    pub explicit_context: Option<String>,
    pub include_parent_canvas: bool,
    pub capabilities: Vec<Capability>,
    pub max_turns: Option<u64>,
    pub max_tool_calls: Option<u64>,
    pub max_tokens: Option<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
pub struct AgentOutcome {
    pub ok: bool,
    pub summary: String,
    pub output: String,
    pub error: Option<String>,
    pub provider: String,
    pub model: String,
    pub child_agent_id: String,
    pub spawn_event_id: String,
    pub result_event_id: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ArtifactWrite {
    pub display_name: String,
    pub media_type: String,
    pub bytes: Vec<u8>,
    pub source_event_ids: Vec<String>,
    pub metadata: JsonObject,
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
pub struct ArtifactRecord {
    pub persisted_event_id: String,
    pub relative_path: String,
    pub sha256: String,
    pub byte_len: usize,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct HostAgentBudget {
    pub max_turns: Option<u32>,
    pub max_tool_calls: Option<u32>,
    pub max_tokens: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct HostAgentTask {
    pub task: String,
    pub persona: String,
    pub provider: String,
    pub model: String,
    pub capabilities: Vec<Capability>,
    pub budget: HostAgentBudget,
    pub result_schema: Option<Value>,
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct HostAgentResult {
    pub ok: bool,
    pub summary: String,
    pub output: Option<String>,
    pub error: Option<String>,
}

impl HostAgentResult {
    pub fn success(summary: impl Into<String>, output: Option<impl Into<String>>) -> Self {
        Self {
            ok: true,
            summary: summary.into(),
            output: output.map(Into::into),
            error: None,
        }
    }

    pub fn failure(
        summary: impl Into<String>,
        error: impl Into<String>,
        output: Option<impl Into<String>>,
    ) -> Self {
        Self {
            ok: false,
            summary: summary.into(),
            output: output.map(Into::into),
            error: Some(error.into()),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
pub struct HostAgentRecord {
    pub child_agent_id: String,
    pub spawn_event_id: String,
    pub result_event_id: String,
}

// ---------------------------------------------------------------------------
// Event-feed checkpoint (mirror of euler_sdk::event_checkpoint).
// ---------------------------------------------------------------------------

pub const EVENT_FEED_CHECKPOINT_SCHEMA_VERSION: u16 = 1;
pub const MAX_EVENT_FEED_CHECKPOINT_CURSOR_BYTES: usize = 128;

#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EventFeedCheckpoint {
    pub schema_version: u16,
    pub after_event_id: String,
}

impl EventFeedCheckpoint {
    pub fn new(after_event_id: impl Into<String>) -> Result<Self, EventFeedCheckpointError> {
        let checkpoint = Self {
            schema_version: EVENT_FEED_CHECKPOINT_SCHEMA_VERSION,
            after_event_id: after_event_id.into(),
        };
        checkpoint.validate()?;
        Ok(checkpoint)
    }

    pub fn validate(&self) -> Result<(), EventFeedCheckpointError> {
        if self.schema_version != EVENT_FEED_CHECKPOINT_SCHEMA_VERSION {
            return Err(EventFeedCheckpointError::UnsupportedSchemaVersion);
        }
        if valid_event_feed_cursor(&self.after_event_id) {
            Ok(())
        } else {
            Err(EventFeedCheckpointError::InvalidCursor)
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EventFeedCheckpointError {
    InvalidCursor,
    UnsupportedSchemaVersion,
}

impl fmt::Display for EventFeedCheckpointError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidCursor => f.write_str("invalid checkpoint cursor"),
            Self::UnsupportedSchemaVersion => f.write_str("unsupported checkpoint schema version"),
        }
    }
}

impl std::error::Error for EventFeedCheckpointError {}

pub fn valid_event_feed_cursor(value: &str) -> bool {
    (1..=MAX_EVENT_FEED_CHECKPOINT_CURSOR_BYTES).contains(&value.len())
        && value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
}

// ---------------------------------------------------------------------------
// Extension error.
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
pub enum ExtensionError {
    Message(String),
    CapabilityDenied { capability: Capability },
    QueryFailed(String),
    DiagnosticsReadFailed(String),
    StateDirFailed(String),
    ArtifactWriteFailed(String),
    CheckpointFailed(String),
    AgentTaskFailed(String),
    ContextSlotFailed(String),
}

impl fmt::Display for ExtensionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Message(message) => write!(f, "{message}"),
            Self::CapabilityDenied { capability } => {
                write!(f, "missing required capability {}", capability.as_str())
            }
            Self::QueryFailed(message) => write!(f, "provenance query failed: {message}"),
            Self::DiagnosticsReadFailed(message) => {
                write!(f, "diagnostics read failed: {message}")
            }
            Self::StateDirFailed(message) => write!(f, "state directory failed: {message}"),
            Self::ArtifactWriteFailed(message) => write!(f, "artifact write failed: {message}"),
            Self::CheckpointFailed(message) => write!(f, "checkpoint failed: {message}"),
            Self::AgentTaskFailed(message) => write!(f, "agent task failed: {message}"),
            Self::ContextSlotFailed(message) => write!(f, "context slot update failed: {message}"),
        }
    }
}

impl std::error::Error for ExtensionError {}

// ---------------------------------------------------------------------------
// Host API surface + descriptor traits.
// ---------------------------------------------------------------------------

pub trait HostApi {
    fn query_provenance(&self, query: ProvenanceQuery) -> Result<ProvenancePage, ExtensionError>;
    fn read_diagnostics(
        &self,
        _query: DiagnosticsQuery,
    ) -> Result<DiagnosticsPage, ExtensionError> {
        Err(ExtensionError::DiagnosticsReadFailed(
            "diagnostics read unavailable".to_owned(),
        ))
    }
    fn state_dir(&self) -> Result<PathBuf, ExtensionError>;
    fn write_artifact(&self, artifact: ArtifactWrite) -> Result<ArtifactRecord, ExtensionError>;
    fn spawn_agent(&self, _task: SpawnAgentTask) -> Result<AgentOutcome, ExtensionError> {
        Err(ExtensionError::Message(
            "agent spawn unavailable on this host".to_owned(),
        ))
    }
    fn spawn_agents(
        &self,
        _tasks: Vec<SpawnAgentTask>,
    ) -> Result<Vec<AgentOutcome>, ExtensionError> {
        Err(ExtensionError::Message(
            "batch agent spawn unavailable on this host".to_owned(),
        ))
    }
    fn load_event_feed_checkpoint(
        &self,
        name: &str,
    ) -> Result<Option<EventFeedCheckpoint>, ExtensionError>;
    fn store_event_feed_checkpoint(
        &self,
        name: &str,
        checkpoint: EventFeedCheckpoint,
    ) -> Result<(), ExtensionError>;
    fn record_agent_task_result(
        &self,
        _task: HostAgentTask,
        _result: HostAgentResult,
    ) -> Result<HostAgentRecord, ExtensionError> {
        Err(ExtensionError::AgentTaskFailed(
            "agent task recording unavailable".to_owned(),
        ))
    }
    fn update_context_slot(&self, _slot: &str, _content: &str) -> Result<(), ExtensionError> {
        Err(ExtensionError::ContextSlotFailed(
            "context slot update unavailable".to_owned(),
        ))
    }
}

pub trait CommandRegistrar {
    fn register_command(&mut self, name: &str, command: Box<dyn ExtensionCommand>);
}

pub trait ExtensionCommand: Send + Sync {
    fn descriptor(&self) -> CommandDescriptor {
        CommandDescriptor {
            invocation: Invocation::User,
            name: String::new(),
            display_name: String::new(),
            summary: String::new(),
            required_capabilities: Vec::new(),
            args: Vec::new(),
            accepts_session_id: false,
        }
    }

    fn execute(&self, context: CommandContext, host: &dyn HostApi)
        -> Result<Value, ExtensionError>;
}

pub trait Extension: Send + Sync {
    fn manifest(&self) -> ExtensionManifest;
    fn register(&self, registrar: &mut dyn CommandRegistrar) -> Result<(), ExtensionError>;
}

// ---------------------------------------------------------------------------
// Managed-process wire adapter.
// ---------------------------------------------------------------------------

/// Adapts an [`euler_managed_process_sdk::Host`] to the bundled [`HostApi`]
/// trait so the ported command bodies run unchanged. Interior mutability lets
/// the `&self` trait methods reach the `&mut Host` wire; the protocol is
/// strictly request/response so no reentrancy occurs.
pub struct WireHost<'host, 'wire, R: BufRead, W: Write> {
    host: RefCell<&'host mut mp::Host<'wire, R, W>>,
}

impl<'host, 'wire, R: BufRead, W: Write> WireHost<'host, 'wire, R, W> {
    pub fn new(host: &'host mut mp::Host<'wire, R, W>) -> Self {
        Self {
            host: RefCell::new(host),
        }
    }
}

fn decode<T: for<'de> Deserialize<'de>>(value: Value, context: &str) -> Result<T, ExtensionError> {
    serde_json::from_value(value)
        .map_err(|error| ExtensionError::Message(format!("{context}: {error}")))
}

impl<R: BufRead, W: Write> HostApi for WireHost<'_, '_, R, W> {
    fn query_provenance(&self, query: ProvenanceQuery) -> Result<ProvenancePage, ExtensionError> {
        let wire_query = mp::ProvenanceQuery {
            after_event_id: query.after_event_id,
            kinds: query.kinds,
            limit: query.limit as u64,
            scan_limit: query.scan_limit as u64,
            include_blob_fields: query.include_blob_fields,
            blob_byte_limit: query.blob_byte_limit as u64,
        };
        let page = self
            .host
            .borrow_mut()
            .query_provenance(&wire_query)
            .map_err(|error| ExtensionError::QueryFailed(error.to_string()))?;
        decode(page, "provenance page")
    }

    fn state_dir(&self) -> Result<PathBuf, ExtensionError> {
        let path = self
            .host
            .borrow_mut()
            .state_dir()
            .map_err(|error| ExtensionError::StateDirFailed(error.to_string()))?;
        Ok(PathBuf::from(path))
    }

    fn write_artifact(&self, artifact: ArtifactWrite) -> Result<ArtifactRecord, ExtensionError> {
        let wire_artifact = mp::ArtifactWrite {
            display_name: artifact.display_name,
            media_type: artifact.media_type,
            bytes: artifact.bytes,
            source_event_ids: artifact.source_event_ids,
            metadata: artifact.metadata,
        };
        let record = self
            .host
            .borrow_mut()
            .write_artifact(&wire_artifact)
            .map_err(|error| ExtensionError::ArtifactWriteFailed(error.to_string()))?;
        decode(record, "artifact record")
    }

    fn spawn_agent(&self, task: SpawnAgentTask) -> Result<AgentOutcome, ExtensionError> {
        let task_value = serde_json::to_value(&task)
            .map_err(|error| ExtensionError::AgentTaskFailed(error.to_string()))?;
        let outcome = self
            .host
            .borrow_mut()
            .spawn_agent(&task_value)
            .map_err(|error| ExtensionError::AgentTaskFailed(error.to_string()))?;
        decode(outcome, "agent outcome")
    }

    fn spawn_agents(
        &self,
        tasks: Vec<SpawnAgentTask>,
    ) -> Result<Vec<AgentOutcome>, ExtensionError> {
        let task_values = tasks
            .iter()
            .map(serde_json::to_value)
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| ExtensionError::AgentTaskFailed(error.to_string()))?;
        let outcomes = self
            .host
            .borrow_mut()
            .spawn_agents(&task_values)
            .map_err(|error| ExtensionError::AgentTaskFailed(error.to_string()))?;
        outcomes
            .into_iter()
            .map(|outcome| decode(outcome, "agent outcome"))
            .collect()
    }

    fn load_event_feed_checkpoint(
        &self,
        name: &str,
    ) -> Result<Option<EventFeedCheckpoint>, ExtensionError> {
        let value = self
            .host
            .borrow_mut()
            .load_checkpoint(name)
            .map_err(|error| ExtensionError::CheckpointFailed(error.to_string()))?;
        decode(value, "event feed checkpoint")
    }

    fn store_event_feed_checkpoint(
        &self,
        name: &str,
        checkpoint: EventFeedCheckpoint,
    ) -> Result<(), ExtensionError> {
        let value = serde_json::to_value(&checkpoint)
            .map_err(|error| ExtensionError::CheckpointFailed(error.to_string()))?;
        self.host
            .borrow_mut()
            .store_checkpoint(name, &value)
            .map_err(|error| ExtensionError::CheckpointFailed(error.to_string()))
    }

    fn record_agent_task_result(
        &self,
        task: HostAgentTask,
        result: HostAgentResult,
    ) -> Result<HostAgentRecord, ExtensionError> {
        let task_value = serde_json::to_value(&task)
            .map_err(|error| ExtensionError::AgentTaskFailed(error.to_string()))?;
        let result_value = serde_json::to_value(&result)
            .map_err(|error| ExtensionError::AgentTaskFailed(error.to_string()))?;
        let record = self
            .host
            .borrow_mut()
            .record_agent_task_result(&task_value, &result_value)
            .map_err(|error| ExtensionError::AgentTaskFailed(error.to_string()))?;
        decode(record, "agent task record")
    }

    fn update_context_slot(&self, slot: &str, content: &str) -> Result<(), ExtensionError> {
        self.host
            .borrow_mut()
            .update_context_slot(slot, content)
            .map_err(|error| ExtensionError::ContextSlotFailed(error.to_string()))
    }
}
