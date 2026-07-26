"""Deterministic text renderers over a v5 ``Artifact``: DOT, Markdown, slot summary.

Three pure functions, no I/O beyond reading the shared ``palette-v5.json`` for
status colours/glyphs. The vocabulary is v5 throughout (§2 per-kind statuses):
``refuted`` is decisive knowledge and is reported under its own heading, never
folded into the dead-end pile. Prior art: the archived Rust ``export/text.rs``
and ``slot_summary.rs`` (euler @ 08ef7a1) — adapted to v5, not down-converted.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .schema import Artifact, Node
from .viewer import load_palette

# §2.2 terminal statuses, split for reporting: a refuted *claim* is knowledge,
# so it earns its own heading; the rest are the dead-end pile. ``blocked`` is
# terminal-while-current (Q3) and rides in the open frontier, not the graveyard.
_DEAD_END_STATUSES = ("dead_end", "abandoned", "superseded")
_OPEN_STATUSES = ("open", "blocked", "inconclusive")

# DOT node shapes by v5 kind (rootness is topology, drawn as a gold circle).
_KIND_SHAPE = {"question": "circle", "investigation": "circle",
               "claim": "diamond", "synthesis": "box"}


def _backbone_parent(art: Artifact) -> Dict[str, str]:
    return {e.to_node: e.from_node for e in art.edges if e.canonical_backbone}


def _backbone_children(art: Artifact) -> Dict[str, List[str]]:
    children: Dict[str, List[str]] = {}
    for e in art.edges:
        if e.canonical_backbone:
            children.setdefault(e.from_node, []).append(e.to_node)
    for kids in children.values():
        kids.sort()
    return children


def _short_event(event_id: str) -> str:
    return event_id if len(event_id) <= 10 else event_id[-8:]


def _evidence_label(node: Node) -> str:
    ids = sorted({_short_event(r.event_id) for r in node.source_refs})
    if not ids:
        return "none"
    if len(ids) <= 3:
        return ", ".join(ids)
    return f"{', '.join(ids[:3])} +{len(ids) - 3}"


def _graph_title(art: Artifact, by_id: Dict[str, Node], roots: List[str]) -> str:
    for candidate in (art.active_root, roots[0] if roots else None):
        if candidate and candidate in by_id:
            return by_id[candidate].title or candidate
    return f"Causal DAG · {art.session.id[:8]}"


# --- DOT ---------------------------------------------------------------------

def _dot_escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\r", "").replace("\n", "\\n"))


def to_dot(artifact: Artifact) -> str:
    """Graphviz digraph: solid backbone, dashed annotations, node colour by status."""
    pal = load_palette()
    statuses = pal["statuses"]
    roots = set(artifact.roots())
    bg = pal["backgrounds"]["day"]
    edge_col = pal["structural_edges"]["day"]
    lines = [
        "digraph causal_dag {",
        f'  graph [rankdir=TB, bgcolor="{bg}", pad=0.25, nodesep=0.35, ranksep=0.6];',
        f'  node [fontname="monospace", style="filled", fillcolor="{bg}"];',
        '  edge [fontname="monospace", fontsize=9];',
    ]
    for n in sorted(artifact.nodes, key=lambda n: n.id):
        is_root = n.id in roots
        if is_root:
            color, glyph = pal["root"]["day"], "○"
        else:
            token = statuses.get(n.status, statuses["open"])
            color, glyph = token["day"], token["glyph"]
        shape = "doublecircle" if is_root else _KIND_SHAPE.get(n.kind, "ellipse")
        pen = "2" if is_root or n.kind == "synthesis" else "1"
        lines.append(
            f'  "{_dot_escape(n.id)}" [label="{_dot_escape(glyph)} '
            f'{_dot_escape(n.title)}", shape={shape}, color="{color}", '
            f'fontcolor="{color}", penwidth={pen}];')
    for e in sorted(artifact.edges, key=lambda e: e.id):
        if not e.canonical_backbone:
            continue
        lines.append(f'  "{_dot_escape(e.from_node)}" -> "{_dot_escape(e.to_node)}" '
                     f'[color="{edge_col}"];')
    arc_kinds = pal["cross_arcs"]["kinds"]
    rest = pal["cross_arcs"]["rest"]
    for e in sorted(artifact.edges, key=lambda e: e.id):
        if e.canonical_backbone:
            continue
        kind_color = arc_kinds.get(e.kind, rest)
        lines.append(
            f'  "{_dot_escape(e.from_node)}" -> "{_dot_escape(e.to_node)}" '
            f'[label="{_dot_escape(e.kind)}", style=dashed, color="{rest}", '
            f'fontcolor="{kind_color}", constraint=false];')
    lines.append("}")
    return "\n".join(lines) + "\n"


# --- Markdown ----------------------------------------------------------------

def _md(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("`", "\\`").replace("*", "\\*")
            .replace("_", "\\_").replace("\r", "").replace("\n", " "))


def _outline(root: str, children, by_id, seen, pal, roots_set, out):
    # Iterative pre-order DFS (Finding 4): a deep valid backbone — e.g. a
    # 1,400-node chain — must render without blowing Python's recursion limit.
    # Children are pushed reversed so they pop in canonical (sorted) order.
    stack = [(root, 0)]
    while stack:
        nid, depth = stack.pop()
        if nid in seen or nid not in by_id:
            continue
        seen.add(nid)
        n = by_id[nid]
        glyph = "○" if nid in roots_set else pal["statuses"].get(
            n.status, pal["statuses"]["open"])["glyph"]
        out.append(f"{'  ' * depth}- {glyph} _{_md(n.status)}_ **{_md(n.title)}**"
                   + (f" — {_md(n.summary)}" if n.summary else ""))
        for child in reversed(children.get(nid, [])):
            stack.append((child, depth + 1))


def _status_block(nodes: List[Node]) -> List[str]:
    if not nodes:
        return ["_None._"]
    return [f"- **{_md(n.title)}** (`{_md(n.status)}`)"
            + (f" — {_md(n.summary)}" if n.summary else "") for n in nodes]


def to_markdown(artifact: Artifact) -> str:
    """Per-root backbone outline, then dead ends, refuted (decided), open frontier, cross-arcs."""
    pal = load_palette()
    by_id = {n.id: n for n in artifact.nodes}
    roots = artifact.roots()
    children = _backbone_children(artifact)
    order = {n.id: i for i, n in enumerate(artifact.nodes)}
    arcs = sorted((e for e in artifact.edges if not e.canonical_backbone),
                  key=lambda e: e.id)
    cross_arc_count = sum(1 for e in arcs if e.edge_class == "annotation")

    out = [
        f"# {_md(_graph_title(artifact, by_id, roots))}",
        "",
        f"- Session: `{_md(artifact.session.id)}`",
        f"- Construction: `{_md(artifact.construction.operation)}`",
        f"- Nodes: {len(artifact.nodes)}",
        f"- Edges: {len(artifact.edges)}",
        f"- Cross-arcs: {cross_arc_count}",
        "",
        "## Backbone",
        "",
    ]
    if not roots:
        out.append("_Empty graph._")
    else:
        seen: set = set()
        roots_set = set(roots)
        for root in roots:
            _outline(root, children, by_id, seen, pal, roots_set, out)

    def by_status(*wanted) -> List[Node]:
        return sorted((n for n in artifact.nodes if n.status in wanted),
                      key=lambda n: order[n.id])

    out += ["", "## Dead ends", ""] + _status_block(by_status(*_DEAD_END_STATUSES))
    out += ["", "## Decided (refuted)", ""] + _status_block(by_status("refuted"))
    out += ["", "## Open frontier", ""] + _status_block(by_status(*_OPEN_STATUSES))

    out += ["", "## Cross-arcs", ""]
    if not arcs:
        out.append("_None._")
    else:
        for e in arcs:
            frm = by_id[e.from_node].title if e.from_node in by_id else e.from_node
            to = by_id[e.to_node].title if e.to_node in by_id else e.to_node
            note = e.basis.summary if e.basis else ""
            out.append(f"- **{_md(e.kind)}:** {_md(frm)} → {_md(to)}"
                       + (f" — {_md(note)}" if note else ""))
    return "\n".join(out) + "\n"


# --- Slot summary ------------------------------------------------------------

_TITLE_BYTES = 120
_ROOT_TITLE_BYTES = 160
_REASON_BYTES = 360
_ACTIVE_LIMIT = 8


def _line_text(value: str) -> str:
    return " ".join(value.split())


def _reason_text(summary: str) -> str:
    line = _line_text(summary)
    for i, ch in enumerate(line):
        if ch in ".!?":
            return line[:i + 1].strip()
    return line.strip()


def _bounded(value: str, max_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    if max_bytes <= 0:
        return ""
    ell = "…"
    if max_bytes < len(ell.encode("utf-8")):
        return ell
    limit = max_bytes - len(ell.encode("utf-8"))
    end = 0
    for i, ch in enumerate(value):
        nxt = len(value[:i + 1].encode("utf-8"))
        if nxt > limit:
            break
        end = i + 1
    return value[:end].rstrip() + ell


def _longest_backbone_path(root: str, children: Dict[str, List[str]]) -> List[str]:
    # Iterative longest-path (Finding 4): recursion crashes on a 1,400-node
    # chain. The backbone is a forest (one parent per node), so an explicit
    # post-order stack with per-node memoized depth is enough. Children are
    # visited in canonical (sorted) order and the first child reaching the
    # maximum depth wins — because distinct child ids differ at position 0,
    # this reproduces the old lexicographic ``candidate < best`` tie-break
    # (smallest starting id) without materializing every candidate path.
    depth: Dict[str, int] = {}
    nxt: Dict[str, Optional[str]] = {}
    stack = [(root, False)]
    while stack:
        nid, processed = stack.pop()
        if processed:
            best_len, best_child = 0, None
            for child in children.get(nid, ()):  # already sorted
                if depth[child] > best_len:
                    best_len, best_child = depth[child], child
            depth[nid] = 1 + best_len
            nxt[nid] = best_child
        else:
            stack.append((nid, True))
            for child in children.get(nid, ()):
                stack.append((child, False))
    path: List[str] = []
    cur: Optional[str] = root
    while cur is not None:
        path.append(cur)
        cur = nxt.get(cur)
    return path


def _backbone_order(art: Artifact, roots: List[str],
                    children: Dict[str, List[str]]) -> Dict[str, int]:
    order: Dict[str, int] = {}
    for root in roots:
        stack = [root]
        while stack:
            nid = stack.pop(0)
            if nid in order:
                continue
            order[nid] = len(order)
            stack.extend(children.get(nid, []))
    return order


_MIN_BUDGET = 512

# Sections carrying a first-sentence reason line beside each title. The rest
# carry a bare title. ``refuted`` is decisive negative knowledge and so earns
# a reason, exactly like the dead-end pile (Finding 1).
_REASON_SECTIONS = ("dead_ends", "refuted")

# Survival priority, lowest first: the order whole sections are surrendered in
# under the final hard byte guarantee, and the mirror of the entry-drop order
# in ``fit`` (Finding 1). ``refuted`` survives longest of all.
_SURVIVAL_ORDER = ("open", "inconclusive", "active", "blocked", "dead_ends", "refuted")


class _Summary:
    """The slot summary as six trim-ordered sections under a GRAPH header.

    Report order (empty sections omitted): DEAD ENDS, REFUTED, BLOCKED,
    INCONCLUSIVE, ACTIVE PATH, OPEN. Decisive negative knowledge — dead ends
    and refuted claims — is a first-class section so it can never vanish when
    it falls off the longest active path (Finding 1).
    """

    _HEADINGS = {
        "dead_ends": "DEAD ENDS", "refuted": "REFUTED", "blocked": "BLOCKED",
        "inconclusive": "INCONCLUSIVE", "active": "ACTIVE PATH", "open": "OPEN",
    }
    _RENDER_ORDER = ("dead_ends", "refuted", "blocked", "inconclusive", "active", "open")

    def __init__(self, artifact: Artifact):
        by_id = {n.id: n for n in artifact.nodes}
        roots = artifact.roots()
        children = _backbone_children(artifact)
        order = _backbone_order(artifact, roots, children)
        active = artifact.active_root or (roots[0] if roots else None)

        root_title = _bounded(
            _graph_title(artifact, by_id, roots), _ROOT_TITLE_BYTES)
        self.header = (f"GRAPH: {root_title} "
                       f"({len(artifact.nodes)} nodes, {len(artifact.edges)} edges)")

        path = _longest_backbone_path(active, children) if active in by_id else []
        active_titles = [_bounded(_line_text(by_id[i].title), _TITLE_BYTES)
                         for i in path if i in by_id]

        ordered = sorted(artifact.nodes, key=lambda n: (order.get(n.id, 1 << 30), n.id))

        def titles(*statuses) -> List[str]:
            return [_bounded(_line_text(n.title), _TITLE_BYTES)
                    for n in ordered if n.status in statuses]

        def reasons(*statuses) -> List[List[str]]:
            return [[_bounded(_line_text(n.title), _TITLE_BYTES),
                     _bounded(_reason_text(n.summary), _REASON_BYTES)]
                    for n in ordered if n.status in statuses]

        # entries[key] is a list (titles) or list of [title, reason] pairs;
        # hidden[key] counts entries dropped, rendered as a "… N more" marker.
        self.entries: Dict[str, List] = {
            "dead_ends": reasons(*_DEAD_END_STATUSES),
            "refuted": reasons("refuted"),
            "blocked": titles("blocked"),
            "inconclusive": titles("inconclusive"),
            "active": list(active_titles[max(0, len(active_titles) - _ACTIVE_LIMIT):]),
            "open": titles("open"),
        }
        self.hidden: Dict[str, int] = {k: 0 for k in self.entries}
        self.hidden["active"] = max(0, len(active_titles) - _ACTIVE_LIMIT)
        self.suppressed: set = set()

    def render(self) -> str:
        lines = [self.header]
        for key in self._RENDER_ORDER:
            if key in self.suppressed:
                continue
            entries, hidden = self.entries[key], self.hidden[key]
            if not entries and hidden == 0:
                continue  # never populated — omit the heading entirely
            lines.append("")
            lines.append(f"{self._HEADINGS[key]}:")
            if hidden > 0:
                lines.append(f"… {hidden} more")
            if key in _REASON_SECTIONS:
                lines.extend(f"- {t}" + (f" — {r}" if r else "") for t, r in entries)
            else:
                lines.extend(f"- {t}" for t in entries)
        return "\n".join(lines)

    def fit(self, budget: int) -> None:
        # Entry-drop order (Finding 1), first surrendered to last: OPEN, then
        # INCONCLUSIVE, then ACTIVE PATH trimmed from the front, then reason
        # lines shortened, then BLOCKED, and only then the decisive pile —
        # dead ends, and refuted last of all.
        self._drop_from_end(budget, "open")
        self._drop_from_end(budget, "inconclusive")
        while self._len() > budget and self.entries["active"]:
            self.entries["active"].pop(0)
            self.hidden["active"] += 1
        while self._len() > budget and self._shorten_longest_reason():
            pass
        self._drop_from_end(budget, "blocked")
        self._drop_from_end(budget, "dead_ends")
        self._drop_from_end(budget, "refuted")
        self._hard_fit(budget)

    def _drop_from_end(self, budget: int, key: str) -> None:
        while self._len() > budget and self.entries[key]:
            self.entries[key].pop()
            self.hidden[key] += 1

    def _hard_fit(self, budget: int) -> None:
        # Final hard guarantee (Finding 6): entry-dropping leaves the fixed
        # headings and "… N more" markers standing, which can still overrun a
        # tiny budget. Surrender whole sections from the lowest survival
        # priority up until it fits. The GRAPH header alone always fits within
        # the 512-byte minimum, so this terminates below budget.
        for key in _SURVIVAL_ORDER:
            if self._len() <= budget:
                return
            self.suppressed.add(key)

    def _shorten_longest_reason(self) -> bool:
        target_list, idx, longest = None, -1, 0
        for key in _REASON_SECTIONS:
            for i, (_, reason) in enumerate(self.entries[key]):
                if len(reason) > longest:
                    target_list, idx, longest = self.entries[key], i, len(reason)
        if target_list is None or longest == 0:
            return False
        target = longest - max(24, longest) // 2
        target_list[idx][1] = _bounded(target_list[idx][1], max(0, target))
        return True

    def _len(self) -> int:
        return len(self.render().encode("utf-8"))


def to_summary(artifact: Artifact, budget: int = 4096) -> str:
    """The context-slot text, fit to ``budget`` bytes with the decisive pile
    surviving longest.

    ``budget`` has a documented 512-byte floor (Finding 6): below it the
    fixed headings alone cannot be honoured, so a smaller value raises
    ``ValueError``. At any accepted budget the returned text is guaranteed
    ``<= budget`` bytes.
    """
    if budget < _MIN_BUDGET:
        raise ValueError("summary budget below the 512-byte minimum")
    summary = _Summary(artifact)
    summary.fit(budget)
    return summary.render()
