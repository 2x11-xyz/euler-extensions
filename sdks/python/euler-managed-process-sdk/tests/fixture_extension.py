#!/usr/bin/env python3
"""Protocol fixture that imports the canonical SDK implementation under test."""

from __future__ import annotations

from pathlib import Path
import sys


SDK_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_DIRECTORY / "src"))

from euler_managed_process_sdk import (  # noqa: E402
    CommandContext,
    HostError,
    ProtocolError,
    serve,
)


def spawn_task(task: str) -> dict[str, object]:
    return {
        "task": task,
        "persona": "",
        "provider": "",
        "model": "",
        "system_prompt": "",
        "explicit_context": None,
        "include_parent_canvas": False,
        "capabilities": [],
        "max_turns": None,
        "max_tool_calls": None,
        "max_tokens": None,
    }


def exercise_host(context: CommandContext) -> dict[str, object]:
    host = context.host
    host.progress("starting", 0.25)
    provenance = host.query_provenance(
        after_event_id="event-1",
        kinds=["tool.result"],
        limit=7,
        scan_limit=19,
        include_blob_fields=True,
        blob_byte_limit=1234,
    )
    diagnostics = host.read_diagnostics(tail_lines=5, max_bytes=600)
    state_directory = host.state_dir()
    artifact = host.write_artifact(
        display_name="proof.json",
        media_type="application/json",
        data=b'{"ok":true}',
        source_event_ids=["event-2"],
        metadata={"kind": "proof"},
    )
    checkpoint = host.load_checkpoint("cursor")
    host.store_checkpoint(
        "cursor",
        {"schema_version": 1, "after_event_id": "event-2"},
    )
    record = host.record_agent_task_result(
        {
            "task": "inspect",
            "persona": "observer",
            "provider": "",
            "model": "",
            "capabilities": [],
            "budget": {},
            "result_schema": None,
        },
        {
            "ok": True,
            "summary": "done",
            "output": "body",
            "error": None,
        },
    )
    host.update_context_slot("fixture", "bounded")
    host.update_plan_presentation(
        revision=3,
        status="active",
        explanation="Exercise the canonical client",
        items=[
            {"step": "Inspect", "status": "completed"},
            {"step": "Verify", "status": "in_progress"},
        ],
    )
    child = host.spawn_agent(spawn_task("one"))
    children = host.spawn_agents([spawn_task("two"), spawn_task("three")])
    host.progress("done")
    return {
        "provenance": provenance,
        "diagnostics": diagnostics,
        "state_directory": state_directory,
        "artifact": artifact,
        "checkpoint": checkpoint,
        "record": record,
        "child": child,
        "children": children,
    }


def catch_host_error(context: CommandContext) -> dict[str, object]:
    try:
        context.host.state_dir()
    except HostError as error:
        return {"caught": str(error)}
    return {"caught": None}


def wait_for_cancel(context: CommandContext) -> dict[str, object]:
    context.host.query_provenance()
    return {"unexpected": True}


def invalid_state_result(context: CommandContext) -> dict[str, object]:
    context.host.state_dir()
    return {"unexpected": True}


def catch_inbound_protocol_error(context: CommandContext) -> dict[str, object]:
    try:
        context.host.state_dir()
    except ProtocolError as error:
        return {"caught": str(error)}
    return {"caught": None}


def echo_input(context: CommandContext) -> dict[str, object]:
    return {"input": context.input}


def fail(_context: CommandContext) -> dict[str, object]:
    raise RuntimeError("private implementation detail")


def non_object(_context: CommandContext) -> object:
    return ["not", "an", "object"]


def oversized(_context: CommandContext) -> dict[str, object]:
    return {"content": "x" * (1024 * 1024)}


def catch_outbound_protocol_error(context: CommandContext) -> dict[str, object]:
    values = {
        "nan": float("nan"),
        "positive-infinity": float("inf"),
        "negative-infinity": float("-inf"),
    }
    try:
        context.host.write_artifact(
            display_name="non-finite.json",
            media_type="application/json",
            data=b"{}",
            metadata={"value": values[context.input["kind"]]},
        )
    except ProtocolError as error:
        return {"caught": str(error)}
    return {"caught": None}


if __name__ == "__main__":
    serve(
        {
            "exercise-host": exercise_host,
            "catch-host-error": catch_host_error,
            "wait-for-cancel": wait_for_cancel,
            "invalid-state-result": invalid_state_result,
            "catch-inbound-protocol-error": catch_inbound_protocol_error,
            "echo-input": echo_input,
            "fail": fail,
            "non-object": non_object,
            "oversized": oversized,
            "catch-outbound-protocol-error": catch_outbound_protocol_error,
        }
    )
