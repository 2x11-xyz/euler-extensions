"""Validation, persistence, and context rendering for a session plan."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from euler_managed_process_sdk import extension_model_text_is_format_safe


SCHEMA_VERSION = 1
STATE_FILENAME = "plan.json"
CONTEXT_SLOT = "plan"
ITEM_STATUSES = ("pending", "in_progress", "completed")
PLAN_STATUSES = ("active", "blocked", "waiting")
MAX_ITEMS = 16
MAX_STEP_CHARACTERS = 256
MAX_EXPLANATION_CHARACTERS = 1024
MAX_STATE_BYTES = 32 * 1024
MAX_CONTEXT_SLOT_BYTES = 4096
MAX_REVISION = (1 << 63) - 1
_SLOT_STEP_BYTES = 176
_SLOT_REASON_BYTES = 480


class PlanValidationError(ValueError):
    """A tool input or durable state document violates the plan contract."""


class DurablePlanError(PlanValidationError):
    """The persisted plan is unreadable or violates its closed schema."""


@dataclass(frozen=True)
class PlanItem:
    step: str
    status: str


@dataclass(frozen=True)
class PlanState:
    revision: int
    plan_status: str
    explanation: str
    plan: Tuple[PlanItem, ...]

    @property
    def complete(self) -> bool:
        return all(item.status == "completed" for item in self.plan)

    def next_item(self) -> Optional[Tuple[int, PlanItem]]:
        for index, item in enumerate(self.plan, start=1):
            if item.status == "in_progress":
                return index, item
        for index, item in enumerate(self.plan, start=1):
            if item.status == "pending":
                return index, item
        return None

    def counts(self) -> Dict[str, int]:
        return {
            status: sum(item.status == status for item in self.plan)
            for status in ITEM_STATUSES
        }


def parse_replacement(value: Any, revision: int) -> PlanState:
    """Validate a full model-tool replacement and assign its durable revision."""

    if not isinstance(value, dict):
        raise PlanValidationError("update_plan input must be an object")
    _require_exact_keys(
        value,
        required={"plan_status", "plan"},
        optional={"explanation"},
        scope="update_plan input",
    )
    if "explanation" in value:
        _validated_text(
            value["explanation"],
            label="update_plan input explanation",
            maximum_characters=MAX_EXPLANATION_CHARACTERS,
            allow_empty=False,
        )
    return _validated_state(
        revision=revision,
        plan_status=value["plan_status"],
        explanation=value.get("explanation", ""),
        plan=value["plan"],
        scope="update_plan input",
    )


def load_plan(state_directory: str) -> Optional[PlanState]:
    """Load one closed-schema plan document, or return None when none exists."""

    path = Path(state_directory) / STATE_FILENAME
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DurablePlanError("durable plan could not be inspected") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise DurablePlanError("durable plan must be a regular file")
    if metadata.st_size > MAX_STATE_BYTES:
        raise DurablePlanError("durable plan exceeds the state size limit")
    try:
        with path.open("rb") as state_file:
            encoded = state_file.read(MAX_STATE_BYTES + 1)
    except OSError as error:
        raise DurablePlanError("durable plan could not be read") from error
    if len(encoded) > MAX_STATE_BYTES:
        raise DurablePlanError("durable plan exceeds the state size limit")
    try:
        text = encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DurablePlanError("durable plan is not valid UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except DurablePlanError:
        raise
    except (ValueError, RecursionError) as error:
        raise DurablePlanError("durable plan is not valid JSON") from error
    if not isinstance(value, dict):
        raise DurablePlanError("durable plan must be an object")
    try:
        _require_exact_keys(
            value,
            required={
                "schema_version",
                "revision",
                "plan_status",
                "explanation",
                "plan",
            },
            optional=set(),
            scope="durable plan",
        )
    except PlanValidationError as error:
        raise DurablePlanError(str(error)) from error
    schema_version = value["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        raise DurablePlanError("durable plan has an unsupported schema version")
    revision = value["revision"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 1 <= revision <= MAX_REVISION
    ):
        raise DurablePlanError("durable plan revision is invalid")
    try:
        return _validated_state(
            revision=revision,
            plan_status=value["plan_status"],
            explanation=value["explanation"],
            plan=value["plan"],
            scope="durable plan",
        )
    except PlanValidationError as error:
        raise DurablePlanError(str(error)) from error


def replace_plan(state_directory: str, value: Any) -> PlanState:
    """Validate and atomically replace the session plan."""

    previous = load_plan(state_directory)
    revision = 1 if previous is None else previous.revision + 1
    if revision > MAX_REVISION:
        raise PlanValidationError("durable plan revision is exhausted")
    state = parse_replacement(value, revision)
    _write_plan(state_directory, state)
    return state


def render_context_slot(state: PlanState) -> str:
    """Render bounded, compact model context from the durable plan."""

    counts = state.counts()
    lines = [
        (
            f"Plan r{state.revision} · {state.plan_status} · "
            f"{counts['completed']}/{len(state.plan)} completed"
        )
    ]
    if state.plan_status in ("blocked", "waiting"):
        lines.append(f"Reason: {_truncate_utf8(state.explanation, _SLOT_REASON_BYTES)}")
    markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
    for index, item in enumerate(state.plan, start=1):
        step = _truncate_utf8(item.step, _SLOT_STEP_BYTES)
        lines.append(f"{index}. {markers[item.status]} {step}")
    rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) > MAX_CONTEXT_SLOT_BYTES:
        raise RuntimeError("plan context renderer exceeded its fixed byte budget")
    return rendered


def _validated_state(
    *,
    revision: int,
    plan_status: Any,
    explanation: Any,
    plan: Any,
    scope: str,
) -> PlanState:
    if not isinstance(plan_status, str) or plan_status not in PLAN_STATUSES:
        raise PlanValidationError(f"{scope} plan_status is invalid")
    explanation_text = _validated_text(
        explanation,
        label=f"{scope} explanation",
        maximum_characters=MAX_EXPLANATION_CHARACTERS,
        allow_empty=True,
    )
    if plan_status in ("blocked", "waiting") and not explanation_text:
        raise PlanValidationError(
            f"{scope} explanation is required when plan_status is {plan_status}"
        )
    if not isinstance(plan, list) or not 1 <= len(plan) <= MAX_ITEMS:
        raise PlanValidationError(f"{scope} plan must contain 1..{MAX_ITEMS} items")

    items: List[PlanItem] = []
    in_progress = 0
    for index, raw_item in enumerate(plan):
        item_scope = f"{scope} plan item {index + 1}"
        if not isinstance(raw_item, dict):
            raise PlanValidationError(f"{item_scope} must be an object")
        _require_exact_keys(
            raw_item,
            required={"step", "status"},
            optional=set(),
            scope=item_scope,
        )
        step = _validated_text(
            raw_item["step"],
            label=f"{item_scope} step",
            maximum_characters=MAX_STEP_CHARACTERS,
            allow_empty=False,
        )
        status = raw_item["status"]
        if not isinstance(status, str) or status not in ITEM_STATUSES:
            raise PlanValidationError(f"{item_scope} status is invalid")
        in_progress += status == "in_progress"
        items.append(PlanItem(step=step, status=status))
    if in_progress > 1:
        raise PlanValidationError(f"{scope} permits at most one in_progress item")
    return PlanState(
        revision=revision,
        plan_status=plan_status,
        explanation=explanation_text,
        plan=tuple(items),
    )


def _validated_text(
    value: Any, *, label: str, maximum_characters: int, allow_empty: bool
) -> str:
    if not isinstance(value, str):
        raise PlanValidationError(f"{label} must be a string")
    if any(text_character_is_unsafe(character) for character in value):
        raise PlanValidationError(
            f"{label} must be a single line without control or format characters"
        )
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise PlanValidationError(f"{label} must not be empty")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PlanValidationError(f"{label} is not valid Unicode text") from error
    if len(normalized) > maximum_characters:
        raise PlanValidationError(f"{label} exceeds {maximum_characters} characters")
    return normalized


def text_character_is_unsafe(character: str) -> bool:
    """Mirror core's frozen Unicode-17 model-text boundary on Python 3.9+."""

    return (
        unicodedata.category(character) == "Cc"
        or not extension_model_text_is_format_safe(character)
    )


def _require_exact_keys(
    value: Dict[str, Any],
    *,
    required: set[str],
    optional: set[str],
    scope: str,
) -> None:
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise PlanValidationError(
            f"{scope} is missing required fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise PlanValidationError(f"{scope} has unknown fields")


def _object_without_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DurablePlanError("durable plan contains a duplicate field")
        value[key] = item
    return value


def _write_plan(state_directory: str, state: PlanState) -> None:
    directory = Path(state_directory)
    if not directory.is_dir():
        raise PlanValidationError("host state directory does not exist")
    document = {
        "schema_version": SCHEMA_VERSION,
        "revision": state.revision,
        "plan_status": state.plan_status,
        "explanation": state.explanation,
        "plan": [
            {"step": item.step, "status": item.status}
            for item in state.plan
        ],
    }
    encoded = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise RuntimeError("validated plan exceeded its fixed state byte budget")

    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".plan-",
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


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    suffix = "…"
    prefix = encoded[: maximum_bytes - len(suffix.encode("utf-8"))]
    return prefix.decode("utf-8", errors="ignore") + suffix
