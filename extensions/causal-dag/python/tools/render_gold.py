#!/usr/bin/env python3
"""Render the gold walk (and the public mini fixture) to every M2 artifact.

Loads the PRIVATE gold data — the annotation export, the session's turn→event
map from ``walk.db``, and the raw event log — projects it to a v5 artifact,
asserts the invariant checker is finding-free, then writes the canonical dump,
the three text renderers, and the four self-contained viewer pages to ``--out``.
The public mini fixture (``tests/test_walk_import.py``) is rendered alongside as
``mini-*.html`` so CI has self-contained example pages without the private data.

Usage:  python3 tools/render_gold.py --out /tmp/m2-out
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))

from causal_dag import (  # noqa: E402
    check, dumps, import_walk, render_html, to_dot, to_markdown, to_summary,
    viewer_payload,
)

TOOL = pathlib.Path("/home/exedev/code/2x11-xyz/causal-dag-annotation-tool")
GOLD_EXPORT = TOOL / "exports" / "gold-current.json"
WALK_DB = TOOL / "walk.db"
SESSION = "01KXBZY130DSAMPT8C72558Z4J"
EVENTS = pathlib.Path.home() / ".euler" / "sessions" / SESSION / "events.jsonl"

_VIEW_SUFFIX = {"top-down": "top-down", "indented": "indented",
                "3d": "3d", "3-5d": "3-5d"}


def _load_gold_steps() -> list:
    con = sqlite3.connect(f"file:{WALK_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT steps_json FROM sessions WHERE session_id = ?", (SESSION,)
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0])["steps"]


def _load_gold_events() -> dict:
    meta = {}
    with EVENTS.open() as fh:
        for line in fh:
            event = json.loads(line)
            meta[event["id"]] = {"kind": event["kind"], "ts": event["ts"]}
    return meta


def _validate_html(html: str, artifact) -> None:
    """Prove the page is self-contained and carries a re-parseable JSON payload."""
    for external in ("<script src=", "fetch(", "unpkg.com", "fonts.googleapis",
                     "__EULER_DAG__", "__EULER_PALETTE__", "__EULER_RUNTIME__"):
        if external in html:
            raise AssertionError(f"page is not self-contained: found {external!r}")
    # raw_decode stops at the end of the JSON value, so semicolons inside string
    # values (titles, notes) never truncate the payload.
    marker = "const __DAG = "
    start = html.index(marker) + len(marker)
    payload, _ = json.JSONDecoder().raw_decode(html, start)
    if payload["schema"] != "euler.causal_dag.viewer.v5":
        raise AssertionError("embedded payload has the wrong schema id")
    if len(payload["nodes"]) != len(artifact.nodes):
        raise AssertionError("embedded payload node count diverges from the artifact")


def _write(out: pathlib.Path, artifact, stem: str) -> None:
    def emit(name: str, text: str) -> None:
        path = out / name
        path.write_text(text)
        print(f"  {name:28s} {len(text.encode()):>9,d} bytes")

    if stem == "gold":
        emit("artifact.json", dumps(artifact))
        emit("gold.dot", to_dot(artifact))
        emit("gold.md", to_markdown(artifact))
        emit("gold.txt", to_summary(artifact))
    for view, suffix in _VIEW_SUFFIX.items():
        html = render_html(artifact, view)
        _validate_html(html, artifact)
        emit(f"{stem}-{suffix}.html", html)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render gold + mini M2 artifacts")
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    if not (GOLD_EXPORT.exists() and WALK_DB.exists() and EVENTS.exists()):
        print("gold data not present — cannot render the gold pages", file=sys.stderr)
        return 2

    export = json.loads(GOLD_EXPORT.read_text())
    gold = import_walk(export, _load_gold_steps(), _load_gold_events())
    findings = check(gold)
    if findings:
        for f in findings:
            print(f"  FINDING {f.invariant}: {f.message}", file=sys.stderr)
        raise SystemExit("gold artifact is not finding-free — refusing to render")
    # Fail fast if the payload's structural invariants are broken.
    payload = viewer_payload(gold)
    seq = {n["id"]: n["sequence"] for n in payload["nodes"]}
    for n in payload["nodes"]:
        assert n["parent"] is None or seq[n["parent"]] < n["sequence"], n["id"]

    print(f"gold ({len(gold.nodes)} nodes, {len(gold.edges)} edges):")
    _write(out, gold, "gold")

    from test_walk_import import EXPORT, STEPS, EVENTS as MINI_EVENTS
    mini = import_walk(EXPORT, STEPS, MINI_EVENTS)
    assert check(mini) == []
    print(f"mini ({len(mini.nodes)} nodes, {len(mini.edges)} edges):")
    _write(out, mini, "mini")

    print(f"\nwrote artifacts to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
