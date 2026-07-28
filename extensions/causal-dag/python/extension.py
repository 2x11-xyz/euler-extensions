#!/usr/bin/env python3
"""Managed-process entrypoint for the causal-dag extension (milestone 3).

The host runs this with the package directory as the working directory and a
deliberately bare environment (no PYTHONPATH), so the interpreter is pointed at
the vendored SDK and the ``causal_dag`` package explicitly, relative to this
file — never an ambient install. Then it hands the three declared commands to
``serve`` and does nothing else: one command per invocation, JSON in, JSON out.
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
# The package (causal_dag) and the vendored SDK both live under this directory.
for _path in (_HERE, _HERE / "_sdk"):
    entry = str(_path)
    if entry not in sys.path:
        sys.path.insert(0, entry)

from euler_managed_process_sdk import serve  # noqa: E402

from causal_dag.commands import HANDLERS  # noqa: E402


def main() -> None:
    serve(HANDLERS)


if __name__ == "__main__":
    main()
