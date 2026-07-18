//! Local mirror of the `euler-event` envelope and event-kind constants.
//!
//! The managed-process wire delivers provenance events as JSON matching the
//! host's `EventEnvelope` serialization (`v/id/ts/session/agent/parent/kind/
//! payload/blobs`). This module re-declares the exact deserialize shape and the
//! stable event-kind string constants so the ported projection/construction
//! modules keep their typed `EventEnvelope`/`EventKind` access verbatim, without
//! depending on the in-tree `euler-event` crate.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::fmt;

pub type JsonObject = Map<String, Value>;
pub type JsonValue = Value;

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Deserialize, Serialize)]
#[serde(transparent)]
pub struct EventKind(String);

impl EventKind {
    pub const USER_MESSAGE: &'static str = "user.message";
    pub const ASSISTANT_MESSAGE: &'static str = "assistant.message";
    pub const ASSISTANT_ACTIVITY: &'static str = "assistant.activity";
    pub const PLAN_UPDATE: &'static str = "plan.update";
    pub const TOOL_CALL: &'static str = "tool.call";
    pub const TOOL_RESULT: &'static str = "tool.result";
    pub const PERMISSION_PROMPT: &'static str = "permission.prompt";
    pub const PERMISSION_DECISION: &'static str = "permission.decision";
    pub const PATCH_PROPOSED: &'static str = "patch.proposed";
    pub const PATCH_APPLIED: &'static str = "patch.applied";
    pub const FILE_CHANGE: &'static str = "file.change";
    pub const FILE_DIFF: &'static str = "file.diff";
    pub const WORKSPACE_RESTORE: &'static str = "workspace.restore";
    pub const CHECK_STARTED: &'static str = "check.started";
    pub const CHECK_RESULT: &'static str = "check.result";
    pub const MODEL_CALL: &'static str = "model.call";
    pub const MODEL_RESULT: &'static str = "model.result";
    pub const MODEL_REASONING: &'static str = "model.reasoning";
    pub const MODEL_DELTA: &'static str = "model.delta";
    pub const MODEL_SWITCHED: &'static str = "model.switched";
    pub const MODEL_EFFORT_CHANGED: &'static str = "model.effort.changed";
    pub const CONTEXT_LIMIT: &'static str = "context.limit";
    pub const CONTEXT_SLOT_UPDATED: &'static str = "context.slot.updated";
    pub const CANVAS_SNAPSHOT: &'static str = "canvas.snapshot";
    pub const CANVAS_POLICY_CHANGED: &'static str = "canvas.policy.changed";
    pub const CANVAS_SWAP: &'static str = "canvas.swap";
    pub const CANVAS_CANDIDATE_DISCARDED: &'static str = "canvas.candidate.discarded";
    pub const SECRET_REDACTED: &'static str = "secret.redacted";
    pub const SECRET_EXPOSURE_DETECTED: &'static str = "secret.exposure.detected";
    pub const SECRET_SCRUBBED: &'static str = "secret.scrubbed";
    pub const EXTENSION_ARTIFACT: &'static str = "extension.artifact";
    pub const AGENT_SPAWN: &'static str = "agent.spawn";
    pub const AGENT_MESSAGE: &'static str = "agent.message";
    pub const AGENT_RESULT: &'static str = "agent.result";
    pub const SESSION_START: &'static str = "session.start";
    pub const SESSION_RESUMED: &'static str = "session.resumed";
    pub const SESSION_RENAMED: &'static str = "session.renamed";
    pub const SESSION_SUMMARY: &'static str = "session.summary";
    pub const ERROR: &'static str = "error";

    pub fn new(kind: impl Into<String>) -> Self {
        Self(kind.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl From<&str> for EventKind {
    fn from(value: &str) -> Self {
        Self::new(value)
    }
}

impl From<String> for EventKind {
    fn from(value: String) -> Self {
        Self::new(value)
    }
}

impl fmt::Display for EventKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Canonical session event envelope, matching the host wire serialization.
#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
pub struct EventEnvelope {
    #[serde(default)]
    pub v: u16,
    pub id: String,
    #[serde(default)]
    pub ts: String,
    #[serde(default)]
    pub session: String,
    #[serde(default)]
    pub agent: String,
    #[serde(default)]
    pub parent: Option<String>,
    pub kind: EventKind,
    #[serde(default)]
    pub payload: JsonObject,
    #[serde(default)]
    pub blobs: BTreeMap<String, String>,
}

impl EventEnvelope {
    pub fn to_json_line(&self) -> serde_json::Result<String> {
        serde_json::to_string(self)
    }

    pub fn from_json_line(line: &str) -> serde_json::Result<Self> {
        serde_json::from_str(line)
    }
}

/// Build a `JsonObject` from static-keyed entries. Mirrors `euler_event::object`
/// so ported fixtures and inline tests construct payloads verbatim.
pub fn object(entries: impl IntoIterator<Item = (&'static str, JsonValue)>) -> JsonObject {
    entries
        .into_iter()
        .map(|(key, value)| (key.to_owned(), value))
        .collect()
}
