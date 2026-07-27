#!/usr/bin/env python3
"""Small proof extension for the canonical Python SDK."""

from pathlib import Path
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


def inspect(context: CommandContext) -> dict[str, object]:
    context.host.progress("reading a bounded provenance page", 0.25)
    page = context.host.query_provenance(limit=8, scan_limit=32)
    event_ids = [event["id"] for event in page["events"]]
    artifact = context.host.write_artifact(
        display_name="python-proof-summary.json",
        media_type="application/json",
        data=("{\"event_count\":%d}" % len(event_ids)).encode("utf-8"),
        source_event_ids=event_ids,
        metadata={"producer": "python-proof"},
    )
    context.host.progress("artifact written", 1.0)
    return {"event_count": len(event_ids), "artifact": artifact}


if __name__ == "__main__":
    serve({"inspect": inspect})
