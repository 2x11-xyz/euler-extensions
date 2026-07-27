#!/usr/bin/env python3
"""Summarize a session using Euler's managed-process Python SDK."""

from pathlib import Path
import json
import sys


_PYTHON_DIR = Path(__file__).resolve().parent
_EXTENSION_DIR = _PYTHON_DIR.parent
_REPOSITORY_DIR = _EXTENSION_DIR.parents[1]
_SDK_SOURCE = (
    _REPOSITORY_DIR
    / "sdks"
    / "python"
    / "euler-managed-process-sdk"
    / "src"
)

if not _SDK_SOURCE.is_dir():
    raise RuntimeError(f"canonical Euler Python SDK not found at {_SDK_SOURCE}")

sys.path.insert(0, str(_SDK_SOURCE))

from euler_managed_process_sdk import CommandContext, serve  # noqa: E402


def summarize(context: CommandContext) -> dict[str, object]:
    context.host.progress("reading provenance", 0.25)
    page = context.host.query_provenance(limit=256, scan_limit=1024)

    counts: dict[str, int] = {}
    for event in page.get("events", []):
        kind = event.get("kind", "unknown")
        counts[kind] = counts.get(kind, 0) + 1

    summary = {
        "event_count": len(page.get("events", [])),
        "event_kinds": counts,
        "truncated": page.get("truncated", False),
    }
    artifact = context.host.write_artifact(
        display_name="python-session-summary.json",
        media_type="application/json",
        data=(json.dumps(summary, indent=2) + "\n").encode("utf-8"),
        metadata={"producer": "python-session-summary"},
    )
    context.host.progress("summary written", 1.0)
    return {"summary": summary, "artifact": artifact}


if __name__ == "__main__":
    serve({"summarize": summarize})
