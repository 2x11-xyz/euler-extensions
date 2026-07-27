#!/usr/bin/env python3
"""Managed-process entrypoint for the plan/todo extension."""

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
sys.path.insert(0, str(_PYTHON_DIR))

from euler_managed_process_sdk import serve  # noqa: E402
from plan_todo.commands import handle_idle, handle_update_plan  # noqa: E402


if __name__ == "__main__":
    serve(
        {
            "update-plan": handle_update_plan,
            "continue-if-needed": handle_idle,
        }
    )
