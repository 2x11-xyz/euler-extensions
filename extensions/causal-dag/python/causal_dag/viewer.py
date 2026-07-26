"""The viewer projection and self-contained HTML assembly (SCHEMA-v5 §6).

``viewer_payload`` folds each node's single backbone parent in, walks the
backbone from the active/first root to assign a parent-before-child
``sequence`` (tie-broken by chronological occurrence — the node's first turn),
and turns every non-backbone edge into a cross-arc. ``render_html`` inlines the
archived viewer shells, ``runtime.js``, the bundled React UMD builds and the v5
palette around that payload to produce one offline page per view — the same
marker-substitution assembly the archived crate's ``export/html.rs`` used, kept
verbatim so the four-view contract survives (chronology drives only 3.5D, roots
are always gold, 2D shows bare glyphs, constellations show dot-only legends).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Optional

from .schema import Artifact, Node

VIEWER_SCHEMA = "euler.causal_dag.viewer.v5"

_VIEWER_DIR = pathlib.Path(__file__).resolve().parents[2] / "viewer"
_PALETTE_PATH = _VIEWER_DIR / "palette-v5.json"

# view id -> shell filename.
VIEWS = {
    "top-down": "top-down.html",
    "indented": "indented-spine.html",
    "3d": "constellation-3d.html",
    "3-5d": "constellation-3-5d.html",
}

_RUNTIME_MARKER = "<!--__EULER_RUNTIME__-->"
_DAG_MARKER = "/*__EULER_DAG__*/"
_PALETTE_MARKER = "/*__EULER_PALETTE__*/"

_CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src '
        "'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src "
        "'unsafe-inline'; font-src data:; img-src data:; connect-src 'none'; "
        'frame-src \'self\' data: blob:;">')


def load_palette() -> Dict[str, Any]:
    """The canonical v5 palette tokens (day/night hexes, glyphs, arc colours)."""
    return json.loads(_PALETTE_PATH.read_text())


def _short_event(event_id: str) -> str:
    return event_id if len(event_id) <= 10 else event_id[-8:]


def _evidence_label(node: Node) -> str:
    ids = sorted({_short_event(r.event_id) for r in node.source_refs})
    if not ids:
        return "none"
    if len(ids) <= 3:
        return ", ".join(ids)
    return f"{', '.join(ids[:3])} +{len(ids) - 3}"


def _occurrence(node: Node) -> Optional[int]:
    """Chronological anchor: the step_id of the node's first owned turn (R1).

    Turns are whole session steps, so the smallest step_id a node owns is its
    first-turn/first-event stream position — the ordering the 3.5D view rides.
    """
    if not node.turns:
        return None
    return min(t.step_id for t in node.turns)


def _assign_sequence(artifact: Artifact, parent: Dict[str, str],
                     occ: Dict[str, Optional[int]]) -> Dict[str, int]:
    """Backbone walk placing every parent before its children; ties break on the
    active root, then chronological occurrence, then id (fully deterministic)."""
    roots = artifact.roots()
    active = artifact.active_root or (roots[0] if roots else None)
    ids = [n.id for n in artifact.nodes]
    pending = set(ids)
    placed: set = set()
    sequence: Dict[str, int] = {}

    def key(nid: str):
        o = occ.get(nid)
        return (0 if nid == active else 1, o is None, o if o is not None else 0, nid)

    while pending:
        ready = [nid for nid in pending
                 if parent.get(nid) is None or parent[nid] in placed]
        pick = min(ready, key=key) if ready else min(pending)
        sequence[pick] = len(sequence)
        placed.add(pick)
        pending.discard(pick)
    return sequence


def _graph_title(artifact: Artifact, by_id: Dict[str, Node], roots: List[str]) -> str:
    for candidate in (artifact.active_root, roots[0] if roots else None):
        if candidate and candidate in by_id:
            return by_id[candidate].title or candidate
    return f"Causal DAG · {artifact.session.id[:8]}"


def viewer_payload(artifact: Artifact) -> Dict[str, Any]:
    """Fold the backbone into per-node parent/sequence; non-backbone edges become arcs."""
    by_id = {n.id: n for n in artifact.nodes}
    roots = artifact.roots()
    root_set = set(roots)
    parent = {e.to_node: e.from_node for e in artifact.edges if e.canonical_backbone}
    occ = {n.id: _occurrence(n) for n in artifact.nodes}
    sequence = _assign_sequence(artifact, parent, occ)

    nodes = []
    for n in sorted(artifact.nodes, key=lambda n: sequence[n.id]):
        entry: Dict[str, Any] = {
            "id": n.id,
            "parent": parent.get(n.id),
            "sequence": sequence[n.id],
            "occurrence": occ[n.id],
            "isRoot": n.id in root_set,
            "status": n.status,
            "kind": n.kind,
            "title": n.title,
            "summary": n.summary,
            "ev": _evidence_label(n),
        }
        if n.kind == "synthesis":
            entry["consolidation"] = bool(n.metadata.get("consolidation"))
        nodes.append(entry)

    arcs = [
        {"id": e.id, "from": e.from_node, "to": e.to_node,
         "class": e.edge_class, "kind": e.kind,
         "note": e.basis.summary if e.basis else ""}
        for e in sorted((e for e in artifact.edges if not e.canonical_backbone),
                        key=lambda e: e.id)
    ]

    return {
        "schema": VIEWER_SCHEMA,
        "session_id": artifact.session.id,
        "title": _graph_title(artifact, by_id, roots),
        "operation": artifact.construction.operation,
        "active_root": artifact.active_root,
        "roots": roots,
        "nodes": nodes,
        "arcs": arcs,
    }


def _read_asset(name: str) -> str:
    return (_VIEWER_DIR / name).read_text()


def _script_safe_json(text: str) -> str:
    return (text.replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def render_html(artifact: Artifact, view: str) -> str:
    """Assemble one self-contained page for ``view`` (a key of ``VIEWS``)."""
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}; expected one of {sorted(VIEWS)}")
    shell = _read_asset(VIEWS[view])
    for marker in (_RUNTIME_MARKER, _DAG_MARKER, _PALETTE_MARKER):
        if shell.count(marker) != 1:
            raise ValueError(f"shell {VIEWS[view]} has an invalid {marker} injection point")

    react = _read_asset("react.production.min.js")
    react_dom = _read_asset("react-dom.production.min.js")
    runtime = _read_asset("runtime.js")
    runtime_block = (
        f"{_CSP}\n<script>\n{react}\n</script>\n<script>\n{react_dom}\n"
        f"</script>\n<script>\n{runtime}\n</script>")

    dag_json = _script_safe_json(json.dumps(viewer_payload(artifact),
                                            ensure_ascii=False, sort_keys=True))
    palette_json = _script_safe_json(_PALETTE_PATH.read_text().strip())

    # Palette before DAG: the DAG carries untrusted model/user text, so inject it
    # last — text that happens to match a marker then stays data, not code.
    return (shell.replace(_RUNTIME_MARKER, runtime_block)
            .replace(_PALETTE_MARKER, palette_json)
            .replace(_DAG_MARKER, dag_json))
