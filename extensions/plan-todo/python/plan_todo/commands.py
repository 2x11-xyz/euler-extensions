"""Managed-process command handlers for the plan/todo workflow."""

from __future__ import annotations

from typing import Any, Dict

from euler_managed_process_sdk import HostError

from .plan import (
    CONTEXT_SLOT,
    DurablePlanError,
    PlanValidationError,
    load_plan,
    render_context_slot,
    replace_plan,
    text_character_is_unsafe,
)

MAX_MODEL_ERROR_BYTES = 512


def handle_update_plan(context: Any) -> Dict[str, Any]:
    """Replace the plan, then publish a concise context projection."""

    state_directory = context.host.state_dir()
    try:
        state = replace_plan(state_directory, context.input)
    except DurablePlanError as error:
        _publish_context(context.host, "")
        return {
            "accepted": False,
            "error": _bounded_model_error(error),
        }
    except PlanValidationError as error:
        return {
            "accepted": False,
            "error": _bounded_model_error(error),
        }
    presentation_published = _publish_plan_presentation(context.host, state)
    context_published = _publish_context(
        context.host,
        "" if state.complete else render_context_slot(state),
    )
    return {
        "accepted": True,
        "revision": state.revision,
        "plan_status": state.plan_status,
        "counts": state.counts(),
        "presentation_published": presentation_published,
        "context_published": context_published,
    }


def handle_idle(context: Any) -> Dict[str, str]:
    """Return the exact terminal-idle envelope dictated by durable state."""

    if context.input != {}:
        raise ValueError("continue-if-needed input must be the empty object")
    try:
        state = load_plan(context.host.state_dir())
    except PlanValidationError:
        _publish_context(context.host, "")
        raise
    if state is None:
        _publish_context(context.host, "")
        return {"action": "stop"}

    _publish_plan_presentation(context.host, state)
    if state.complete:
        _publish_context(context.host, "")
        return {"action": "stop"}

    _publish_context(context.host, render_context_slot(state))
    if state.plan_status in ("blocked", "waiting"):
        return {"action": "stop"}

    next_item = state.next_item()
    if next_item is None:
        raise RuntimeError("active incomplete plan has no actionable item")
    index, item = next_item
    return {
        "action": "continue",
        "input": (
            f"Continue the active plan with item {index}: {item.step}. "
            "Keep the plan current with update_plan as statuses change. "
            "Stop only when every item is completed or the plan is explicitly "
            "blocked or waiting."
        ),
    }


def _publish_context(host: Any, content: str) -> bool:
    """Best-effort projection; durable plan state remains authoritative."""

    try:
        host.update_context_slot(CONTEXT_SLOT, content)
    except HostError:
        return False
    return True


def _publish_plan_presentation(host: Any, state: Any) -> bool:
    completed = state.complete
    try:
        host.update_plan_presentation(
            revision=state.revision,
            status="completed" if completed else state.plan_status,
            explanation=None if completed else state.explanation or None,
            items=[
                {"step": item.step, "status": item.status}
                for item in state.plan
            ],
        )
    except HostError:
        return False
    return True


def _bounded_model_error(error: PlanValidationError) -> str:
    safe_message = "".join(
        "\N{REPLACEMENT CHARACTER}"
        if text_character_is_unsafe(character)
        else character
        for character in str(error)
    )
    encoded = safe_message.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_MODEL_ERROR_BYTES:
        return encoded.decode("utf-8")
    prefix = encoded[: MAX_MODEL_ERROR_BYTES - 3].decode("utf-8", errors="ignore")
    return f"{prefix}..."
