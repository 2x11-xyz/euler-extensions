"""Bounded durable state for the deterministic run-health fold."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .config import ConfigurationError, DEFAULT_THRESHOLDS, parse_thresholds
from .policy import MAX_CHECKPOINT_SIGNALS, checkpoint_signature


STATE_FILENAME = "run-health-state.json"
STATE_SCHEMA_VERSION = 1
MAX_STATE_BYTES = 256 * 1024


class DurableStateError(ValueError):
    """The extension's durable state is unreadable or incompatible."""


def new_state() -> Dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "after_event_id": None,
        "root_agent": None,
        "revision": 0,
        "observed_sequence": 0,
        "failure_tracks": {},
        "edit_tracks": {},
        "call_classes": {},
        "open_runs": {},
        "open_root_calls": {},
        "waiting_permission": False,
        "last_progress_event_id": None,
        "last_progress_ts": None,
        "last_progress_sequence": None,
        "latest_observed_event_id": None,
        "latest_observed_ts": None,
        "no_progress_alerted_for": None,
        "usage_samples": [],
        "usage_target_fingerprint": None,
        "context_alerted": False,
        "pending_checkpoint": None,
        "active_checkpoint": None,
        "pending_retirement": None,
    }


def load_state(state_directory: str) -> Dict[str, Any]:
    path = Path(state_directory) / STATE_FILENAME
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return new_state()
    except OSError as error:
        raise DurableStateError("run-health state could not be inspected") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise DurableStateError("run-health state must be a regular file")
    if metadata.st_size > MAX_STATE_BYTES:
        raise DurableStateError("run-health state exceeds its size limit")
    try:
        encoded = path.read_bytes()
    except OSError as error:
        raise DurableStateError("run-health state could not be read") from error
    if len(encoded) > MAX_STATE_BYTES:
        raise DurableStateError("run-health state exceeds its size limit")
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise DurableStateError("run-health state is not valid JSON") from error
    _validate_state(value)
    return value


def store_state(state_directory: str, state: Dict[str, Any]) -> None:
    _validate_state(state)
    directory = Path(state_directory)
    if not directory.is_dir():
        raise DurableStateError("host state directory does not exist")
    encoded = (
        json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise DurableStateError("run-health state exceeds its size limit")

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".run-health-",
            suffix=".tmp",
            dir=str(directory),
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, directory / STATE_FILENAME)
        temporary_path = None
        directory_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _validate_state(value: Any) -> None:
    template = new_state()
    if not isinstance(value, dict) or set(value) != set(template):
        raise DurableStateError("run-health state has an incompatible shape")
    if value["schema_version"] != STATE_SCHEMA_VERSION or isinstance(
        value["schema_version"], bool
    ):
        raise DurableStateError("run-health state has an unsupported schema version")
    for name in ("revision", "observed_sequence"):
        if not _is_int(value[name]) or not 0 <= value[name] < (1 << 63):
            raise DurableStateError(f"run-health state {name} is invalid")
    for name in (
        "failure_tracks",
        "edit_tracks",
        "call_classes",
        "open_runs",
        "open_root_calls",
    ):
        if not isinstance(value[name], dict):
            raise DurableStateError(f"run-health state {name} is invalid")
    if not isinstance(value["usage_samples"], list):
        raise DurableStateError("run-health state usage_samples is invalid")
    for name in ("waiting_permission", "context_alerted"):
        if not isinstance(value[name], bool):
            raise DurableStateError(f"run-health state {name} is invalid")
    for name in (
        "after_event_id",
        "root_agent",
        "last_progress_event_id",
        "last_progress_ts",
        "latest_observed_event_id",
        "latest_observed_ts",
        "no_progress_alerted_for",
        "usage_target_fingerprint",
    ):
        if value[name] is not None and not isinstance(value[name], str):
            raise DurableStateError(f"run-health state {name} is invalid")
    _validate_optional_identifier(value["after_event_id"], "after_event_id")
    _validate_optional_identifier(value["root_agent"], "root_agent")
    _validate_optional_identifier(
        value["last_progress_event_id"], "last_progress_event_id"
    )
    _validate_optional_identifier(
        value["latest_observed_event_id"], "latest_observed_event_id"
    )
    _validate_optional_identifier(
        value["no_progress_alerted_for"], "no_progress_alerted_for"
    )
    if value["usage_target_fingerprint"] is not None:
        _require_hash(value["usage_target_fingerprint"], "usage target fingerprint")
    for name in ("last_progress_ts", "latest_observed_ts"):
        _validate_optional_timestamp(value[name], name)
    if value["last_progress_sequence"] is not None and (
        not _is_int(value["last_progress_sequence"])
        or not 1 <= value["last_progress_sequence"] <= value["observed_sequence"]
    ):
        raise DurableStateError("run-health last progress sequence is invalid")
    if (value["last_progress_event_id"] is None) != (value["last_progress_ts"] is None):
        raise DurableStateError("run-health progress identity and timestamp disagree")
    if (value["last_progress_event_id"] is None) != (
        value["last_progress_sequence"] is None
    ):
        raise DurableStateError("run-health progress identity and sequence disagree")
    if (value["latest_observed_event_id"] is None) != (
        value["latest_observed_ts"] is None
    ):
        raise DurableStateError("run-health observation identity and timestamp disagree")
    if (value["observed_sequence"] == 0) != (
        value["latest_observed_event_id"] is None
    ):
        raise DurableStateError("run-health observation identity and sequence disagree")
    if (
        value["no_progress_alerted_for"] is not None
        and value["last_progress_event_id"] is None
    ):
        raise DurableStateError("run-health no-progress alert has no progress anchor")
    _validate_failure_tracks(value["failure_tracks"])
    _validate_edit_tracks(value["edit_tracks"])
    _validate_call_classes(value["call_classes"])
    _validate_open_runs(value["open_runs"])
    _validate_open_root_calls(value["open_root_calls"])
    _validate_usage_samples(value["usage_samples"])
    if value["usage_samples"] and value["usage_target_fingerprint"] is None:
        raise DurableStateError("run-health usage samples have no model target")
    if value["context_alerted"] and not value["usage_samples"]:
        raise DurableStateError("run-health context alert has no usage evidence")
    _validate_observed_sequence_bounds(value)
    _validate_surface_state(value)
    _validate_unowned_state(value)


def _validate_unowned_state(state: Dict[str, Any]) -> None:
    if state["root_agent"] is not None:
        return
    empty_fields = (
        "failure_tracks",
        "edit_tracks",
        "call_classes",
        "open_runs",
        "open_root_calls",
        "usage_samples",
    )
    optional_fields = (
        "last_progress_event_id",
        "last_progress_ts",
        "last_progress_sequence",
        "latest_observed_event_id",
        "latest_observed_ts",
        "no_progress_alerted_for",
        "usage_target_fingerprint",
        "pending_checkpoint",
        "active_checkpoint",
        "pending_retirement",
    )
    if (
        state["observed_sequence"] != 0
        or state["revision"] != 0
        or state["waiting_permission"]
        or state["context_alerted"]
        or any(state[name] for name in empty_fields)
        or any(state[name] is not None for name in optional_fields)
    ):
        raise DurableStateError("run-health unowned state contains root observations")


def _validate_failure_tracks(tracks: Dict[str, Any]) -> None:
    if len(tracks) > 32:
        raise DurableStateError("run-health state has too many failure tracks")
    for key, track in tracks.items():
        _require_hash(key, "failure track identity")
        if not isinstance(track, dict) or set(track) != {
            "family",
            "count",
            "event_ids",
            "alerted",
            "first_seen_sequence",
            "last_seen_sequence",
        }:
            raise DurableStateError("run-health failure track is invalid")
        _require_hash(track["family"], "failure family")
        _require_positive_int(track["count"], "failure count")
        _validate_event_ids(track["event_ids"])
        if not isinstance(track["alerted"], bool):
            raise DurableStateError("run-health failure alert flag is invalid")
        _validate_sequence_range(track, "failure")


def _validate_edit_tracks(tracks: Dict[str, Any]) -> None:
    if len(tracks) > 64:
        raise DurableStateError("run-health state has too many edit tracks")
    for key, track in tracks.items():
        _require_hash(key, "edit track identity")
        if not isinstance(track, dict) or set(track) != {
            "count",
            "event_ids",
            "alerted",
            "first_seen_sequence",
            "last_seen_sequence",
        }:
            raise DurableStateError("run-health edit track is invalid")
        _require_positive_int(track["count"], "edit count")
        _validate_event_ids(track["event_ids"])
        if not isinstance(track["alerted"], bool):
            raise DurableStateError("run-health edit alert flag is invalid")
        _validate_sequence_range(track, "edit")


def _validate_call_classes(classes: Dict[str, Any]) -> None:
    if len(classes) > 256:
        raise DurableStateError("run-health state has too many call classes")
    for call_id, call_class in classes.items():
        _require_hash(call_id, "tool call identity")
        if not isinstance(call_class, dict) or set(call_class) != {
            "family",
            "validation",
            "validation_requires_exit_code",
            "last_seen_sequence",
        }:
            raise DurableStateError("run-health tool call class is invalid")
        _require_hash(call_class["family"], "tool family")
        if not isinstance(call_class["validation"], bool):
            raise DurableStateError("run-health validation classification is invalid")
        if not isinstance(call_class["validation_requires_exit_code"], bool):
            raise DurableStateError("run-health validation completion rule is invalid")
        if call_class["validation_requires_exit_code"] and not call_class["validation"]:
            raise DurableStateError("run-health validation completion rule is invalid")
        _require_positive_int(call_class["last_seen_sequence"], "call recency")


def _validate_open_runs(runs: Dict[str, Any]) -> None:
    if len(runs) > 16:
        raise DurableStateError("run-health state has too many open runs")
    for run_id, run in runs.items():
        _require_identifier(run_id, "run identity")
        if not isinstance(run, dict) or set(run) != {
            "messages",
            "alerted_count",
            "event_ids",
            "last_input_sequence",
        }:
            raise DurableStateError("run-health open run is invalid")
        for name in ("messages", "alerted_count"):
            if not _is_int(run[name]) or not 0 <= run[name] <= (1 << 31):
                raise DurableStateError(f"run-health open run {name} is invalid")
        accepted_inputs = max(0, run["messages"] - 1)
        if run["alerted_count"] > accepted_inputs:
            raise DurableStateError("run-health open run alert count is invalid")
        _validate_event_ids(run["event_ids"], allow_empty=True)
        if not _is_int(run["last_input_sequence"]) or not 0 <= run[
            "last_input_sequence"
        ] < (1 << 63):
            raise DurableStateError("run-health open run input sequence is invalid")
        if accepted_inputs == 0 and run["last_input_sequence"] != 0:
            raise DurableStateError("run-health open run input sequence is invalid")
        if accepted_inputs > 0 and run["last_input_sequence"] == 0:
            raise DurableStateError("run-health open run input sequence is invalid")


def _validate_open_root_calls(calls: Dict[str, Any]) -> None:
    if len(calls) > 16:
        raise DurableStateError("run-health state has too many open model calls")
    for agent_id, event_id in calls.items():
        _require_identifier(agent_id, "agent identity")
        _require_identifier(event_id, "model call event identity")


def _validate_usage_samples(samples: Any) -> None:
    if not isinstance(samples, list) or len(samples) > 8:
        raise DurableStateError("run-health usage samples are invalid")
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {
            "event_id",
            "input_tokens",
            "cached_tokens",
            "sequence",
        }:
            raise DurableStateError("run-health usage sample is invalid")
        _require_identifier(sample["event_id"], "usage event identity")
        if (
            not _is_int(sample["input_tokens"])
            or not 0 <= sample["input_tokens"] < (1 << 63)
        ):
            raise DurableStateError("run-health input token sample is invalid")
        cached = sample["cached_tokens"]
        if cached is not None and (
            not _is_int(cached) or not 0 <= cached <= sample["input_tokens"]
        ):
            raise DurableStateError("run-health cache token sample is invalid")
        _require_positive_int(sample["sequence"], "usage sequence")


def _validate_surface_state(state: Dict[str, Any]) -> None:
    transitions = [
        state["pending_checkpoint"],
        state["active_checkpoint"],
        state["pending_retirement"],
    ]
    if sum(value is not None for value in transitions) > 1:
        raise DurableStateError("run-health surface transitions overlap")
    if state["revision"] == 0 and any(value is not None for value in transitions):
        raise DurableStateError("run-health revision zero cannot own a surface")

    pending = state["pending_checkpoint"]
    if pending is not None:
        expected = _checkpoint_fields() | {
            "artifact_event_id",
            "plan_published",
            "context_published",
        }
        _validate_checkpoint(pending, state, expected, "pending")
        _validate_optional_identifier(
            pending["artifact_event_id"], "artifact event identity"
        )
        for name in ("plan_published", "context_published"):
            if not isinstance(pending[name], bool):
                raise DurableStateError(f"run-health pending {name} is invalid")
        if pending["plan_published"] and pending["artifact_event_id"] is None:
            raise DurableStateError("run-health plan has no checkpoint artifact")
        if pending["context_published"] and not pending["plan_published"]:
            raise DurableStateError("run-health context has no checkpoint plan")

    active = state["active_checkpoint"]
    if active is not None:
        _validate_checkpoint(
            active,
            state,
            _checkpoint_fields() | {"context_published"},
            "active",
        )
        if not isinstance(active["context_published"], bool):
            raise DurableStateError(
                "run-health active context publication is invalid"
            )

    retirement = state["pending_retirement"]
    if retirement is not None:
        expected = {
            "revision",
            "source_signature",
            "plan_completed",
            "context_cleared",
        }
        if not isinstance(retirement, dict) or set(retirement) != expected:
            raise DurableStateError("run-health pending retirement is invalid")
        if (
            not _is_int(retirement["revision"])
            or retirement["revision"] < 1
            or retirement["revision"] != state["revision"]
        ):
            raise DurableStateError("run-health retirement revision is invalid")
        _require_hash(retirement["source_signature"], "retirement source signature")
        for name in ("plan_completed", "context_cleared"):
            if not isinstance(retirement[name], bool):
                raise DurableStateError(f"run-health retirement {name} is invalid")
        if retirement["plan_completed"] and not retirement["context_cleared"]:
            raise DurableStateError("run-health retirement effects are out of order")


def _checkpoint_fields() -> set[str]:
    return {
        "revision",
        "signature",
        "cutoff_event_id",
        "signals",
        "thresholds",
    }


def _validate_checkpoint(
    checkpoint: Any,
    state: Dict[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if not isinstance(checkpoint, dict) or set(checkpoint) != expected:
        raise DurableStateError(f"run-health {label} checkpoint is invalid")
    revision = checkpoint["revision"]
    if not _is_int(revision) or revision < 1 or revision != state["revision"]:
        raise DurableStateError(f"run-health {label} revision is invalid")
    _require_hash(checkpoint["signature"], f"{label} checkpoint signature")
    _require_identifier(checkpoint["cutoff_event_id"], f"{label} checkpoint cutoff")
    thresholds = checkpoint["thresholds"]
    if not isinstance(thresholds, dict) or set(thresholds) != set(DEFAULT_THRESHOLDS):
        raise DurableStateError(f"run-health {label} thresholds are invalid")
    try:
        parse_thresholds({"schema_version": 1, **thresholds})
    except ConfigurationError as error:
        raise DurableStateError(
            f"run-health {label} threshold value is invalid"
        ) from error
    signals = checkpoint["signals"]
    if (
        not isinstance(signals, list)
        or not 1 <= len(signals) <= MAX_CHECKPOINT_SIGNALS
    ):
        raise DurableStateError(f"run-health {label} signals are invalid")
    for signal in signals:
        _validate_signal(signal)
        if signal["_sequence"] > state["observed_sequence"]:
            raise DurableStateError("run-health signal exceeds observed history")
    expected_signature = checkpoint_signature(
        revision,
        checkpoint["cutoff_event_id"],
        signals,
        thresholds,
    )
    if checkpoint["signature"] != expected_signature:
        raise DurableStateError(f"run-health {label} signature is invalid")


def _validate_signal(signal: Any) -> None:
    if not isinstance(signal, dict):
        raise DurableStateError("run-health signal is invalid")
    kind = signal.get("kind")
    base = {"kind", "count", "event_ids", "_entity", "_sequence"}
    allowed = {
        "repeated_failure": base,
        "edit_thrashing": base,
        "no_meaningful_progress": base,
        "active_run_input": base,
        "context_cache_churn": base
        | {"input_growth_tokens", "maximum_observed_cache_reuse_percent"},
    }
    if kind not in allowed or set(signal) != allowed[kind]:
        raise DurableStateError("run-health signal shape is invalid")
    _require_positive_int(signal["count"], "signal count")
    _require_hash(signal["_entity"], "signal identity")
    _require_positive_int(signal["_sequence"], "signal recency")
    _validate_event_ids(signal["event_ids"])
    if kind == "context_cache_churn":
        _require_positive_int(signal["input_growth_tokens"], "context growth")
        reuse = signal["maximum_observed_cache_reuse_percent"]
        if not _is_int(reuse) or not 0 <= reuse <= 100:
            raise DurableStateError("run-health cache reuse evidence is invalid")


def _validate_observed_sequence_bounds(state: Dict[str, Any]) -> None:
    observed = state["observed_sequence"]
    sequences = []
    for track in state["failure_tracks"].values():
        sequences.extend((track["first_seen_sequence"], track["last_seen_sequence"]))
    for track in state["edit_tracks"].values():
        sequences.extend((track["first_seen_sequence"], track["last_seen_sequence"]))
    sequences.extend(item["last_seen_sequence"] for item in state["call_classes"].values())
    sequences.extend(
        run["last_input_sequence"]
        for run in state["open_runs"].values()
        if run["last_input_sequence"]
    )
    sequences.extend(sample["sequence"] for sample in state["usage_samples"])
    if any(sequence > observed for sequence in sequences):
        raise DurableStateError("run-health state exceeds observed history")


def _validate_sequence_range(track: Dict[str, Any], label: str) -> None:
    first = track["first_seen_sequence"]
    last = track["last_seen_sequence"]
    _require_positive_int(first, f"{label} first recency")
    _require_positive_int(last, f"{label} last recency")
    if first > last:
        raise DurableStateError(f"run-health {label} recency is invalid")


def _validate_event_ids(event_ids: Any, *, allow_empty: bool = False) -> None:
    minimum = 0 if allow_empty else 1
    if not isinstance(event_ids, list) or not minimum <= len(event_ids) <= 8:
        raise DurableStateError("run-health evidence event identities are invalid")
    for event_id in event_ids:
        _require_identifier(event_id, "evidence event identity")


def _validate_optional_identifier(value: Any, label: str) -> None:
    if value is not None:
        _require_identifier(value, label)


def _require_identifier(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 128
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise DurableStateError(f"run-health {label} is invalid")


def _validate_optional_timestamp(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value.encode("utf-8")) > 64:
        raise DurableStateError(f"run-health state {label} is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise DurableStateError(f"run-health state {label} is invalid") from error
    if parsed.tzinfo is None:
        raise DurableStateError(f"run-health state {label} is invalid")


def _require_hash(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DurableStateError(f"run-health {label} is invalid")


def _require_positive_int(value: Any, label: str) -> None:
    if not _is_int(value) or not 1 <= value < (1 << 63):
        raise DurableStateError(f"run-health {label} is invalid")


def _is_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)
