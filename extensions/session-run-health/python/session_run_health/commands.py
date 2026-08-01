"""Managed-process request-tick orchestration for session run health."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from euler_managed_process_sdk import Cancelled, HostError

from .config import load_thresholds
from .policy import (
    CONTEXT_SLOT,
    OBSERVED_KINDS,
    activate_pending_checkpoint,
    artifact_document,
    collect_signals,
    discard_or_retire_stale_checkpoint,
    evidence_event_ids,
    fold_events,
    make_pending_checkpoint,
    plan_presentation,
    render_context_slot,
    retirement_plan_presentation,
)
from .state import load_state, store_state


PAGE_EVENT_LIMIT = 8
PAGE_SCAN_LIMIT = 1024
MAX_QUERY_PAGES = 48


def handle_request_tick(context: Any) -> Dict[str, Any]:
    """Clear model-facing state before any non-cancellation failure escapes."""

    try:
        return _handle_request_tick(context)
    except Cancelled:
        raise
    except Exception:
        _best_effort_clear_context(context.host)
        raise


def _handle_request_tick(context: Any) -> Dict[str, Any]:
    """Consume one host-bounded prefix and publish an advisory when warranted."""

    through_event_id = _parse_tick_input(context.input)
    state_directory = context.host.state_dir()
    thresholds = load_thresholds(state_directory)
    state = load_state(state_directory)

    caught_up, pages = _consume_bounded_feed(
        context.host,
        state,
        through_event_id,
        thresholds,
    )
    store_state(state_directory, state)

    published = False
    retired = False
    if caught_up:
        if discard_or_retire_stale_checkpoint(state):
            store_state(state_directory, state)
        if state["pending_retirement"] is not None:
            retired = _publish_retirement(context.host, state_directory, state)
        elif state["pending_checkpoint"] is not None:
            published = _publish_pending(context.host, state_directory, state)
        elif state["active_checkpoint"] is not None:
            _publish_active_context(context.host, state_directory, state)
    if caught_up and not retired and not _owns_checkpoint_transition(state):
        signals = collect_signals(state, thresholds)
        if signals:
            assessed_cutoff = state["after_event_id"] or through_event_id
            make_pending_checkpoint(state, signals, assessed_cutoff, thresholds)
            store_state(state_directory, state)
            published = _publish_pending(context.host, state_directory, state)

    pending = state["pending_checkpoint"]
    return {
        "assessed_through_event_id": state["after_event_id"],
        "requested_through_event_id": through_event_id,
        "caught_up": caught_up,
        "pages": pages,
        "checkpoint_published": published,
        "checkpoint_pending": pending is not None,
        "checkpoint_active": state["active_checkpoint"] is not None,
        "checkpoint_retiring": state["pending_retirement"] is not None,
        "checkpoint_retired": retired,
        "checkpoint_revision": state["revision"] if state["revision"] else None,
    }


def _owns_checkpoint_transition(state: Dict[str, Any]) -> bool:
    return any(
        state[name] is not None
        for name in ("pending_checkpoint", "active_checkpoint", "pending_retirement")
    )


def _best_effort_clear_context(host: Any) -> None:
    try:
        host.update_context_slot(CONTEXT_SLOT, "")
    except Exception:
        pass


def _parse_tick_input(value: Any) -> str:
    if not isinstance(value, dict) or set(value) != {"through_event_id"}:
        raise ValueError("request tick input must contain only through_event_id")
    event_id = value["through_event_id"]
    if not isinstance(event_id, str) or not event_id or len(event_id.encode("utf-8")) > 128:
        raise ValueError("request tick through_event_id is invalid")
    return event_id


def _consume_bounded_feed(
    host: Any,
    state: Dict[str, Any],
    through_event_id: str,
    thresholds: Dict[str, int],
) -> Tuple[bool, int]:
    cursor = state["after_event_id"]
    if cursor == through_event_id:
        return True, 0

    for page_number in range(1, MAX_QUERY_PAGES + 1):
        page = host.query_provenance(
            after_event_id=cursor,
            through_event_id=through_event_id,
            kinds=OBSERVED_KINDS,
            limit=PAGE_EVENT_LIMIT,
            scan_limit=PAGE_SCAN_LIMIT,
            include_blob_fields=False,
        )
        events, truncated, watermark, continuation = _parse_page(page)
        fold_events(state, events, thresholds)
        if truncated:
            if continuation != watermark:
                raise ValueError("bounded provenance continuation disagrees with watermark")
            if continuation == cursor:
                raise ValueError("bounded provenance page did not advance")
            cursor = continuation
            state["after_event_id"] = cursor
            continue
        if continuation is not None:
            raise ValueError("complete provenance page has a continuation")
        if watermark != through_event_id:
            raise ValueError("bounded provenance page did not reach its cutoff")
        state["after_event_id"] = through_event_id
        return True, page_number
    return False, MAX_QUERY_PAGES


def _parse_page(
    value: Any,
) -> Tuple[List[Dict[str, Any]], bool, str, Any]:
    if not isinstance(value, dict):
        raise ValueError("bounded provenance page must be an object")
    events = value.get("events")
    truncated = value.get("truncated")
    watermark = value.get("watermark_event_id")
    continuation = value.get("next_after_event_id")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise ValueError("bounded provenance page events are invalid")
    if not isinstance(truncated, bool):
        raise ValueError("bounded provenance page truncation is invalid")
    if not isinstance(watermark, str) or not watermark:
        raise ValueError("bounded provenance page watermark is invalid")
    if continuation is not None and not isinstance(continuation, str):
        raise ValueError("bounded provenance page continuation is invalid")
    return events, truncated, watermark, continuation


def _publish_pending(host: Any, state_directory: str, state: Dict[str, Any]) -> bool:
    pending = state["pending_checkpoint"]
    if pending is None:
        return False
    try:
        if pending["artifact_event_id"] is None:
            document = artifact_document(pending)
            encoded = (
                json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode("utf-8")
            artifact = host.write_artifact(
                display_name=f"run-health-checkpoint-r{pending['revision']}.json",
                media_type="application/json",
                data=encoded,
                source_event_ids=evidence_event_ids(pending),
                metadata={
                    "kind": "run-health-checkpoint",
                    "schema_version": 1,
                    "signature": pending["signature"],
                },
            )
            persisted_event_id = (
                artifact.get("persisted_event_id") if isinstance(artifact, dict) else None
            )
            if not isinstance(persisted_event_id, str) or not persisted_event_id:
                raise ValueError("host returned an invalid checkpoint artifact record")
            pending["artifact_event_id"] = persisted_event_id
            store_state(state_directory, state)

        if not pending["plan_published"]:
            host.update_plan_presentation(**plan_presentation(pending))
            pending["plan_published"] = True
            store_state(state_directory, state)

        if not pending["context_published"]:
            host.update_context_slot(CONTEXT_SLOT, render_context_slot(pending))
            pending["context_published"] = True
            store_state(state_directory, state)
    except HostError:
        return False

    activate_pending_checkpoint(state)
    store_state(state_directory, state)
    return True


def _publish_active_context(
    host: Any, state_directory: str, state: Dict[str, Any]
) -> bool:
    active = state["active_checkpoint"]
    if active is None or active["context_published"]:
        return False
    try:
        host.update_context_slot(CONTEXT_SLOT, render_context_slot(active))
    except HostError:
        return False
    active["context_published"] = True
    store_state(state_directory, state)
    return True


def _publish_retirement(host: Any, state_directory: str, state: Dict[str, Any]) -> bool:
    retirement = state["pending_retirement"]
    if retirement is None:
        return False
    try:
        if not retirement["context_cleared"]:
            host.update_context_slot(CONTEXT_SLOT, "")
            retirement["context_cleared"] = True
            store_state(state_directory, state)
        if not retirement["plan_completed"]:
            host.update_plan_presentation(**retirement_plan_presentation(retirement))
            retirement["plan_completed"] = True
            store_state(state_directory, state)
    except HostError:
        return False

    state["pending_retirement"] = None
    store_state(state_directory, state)
    return True
