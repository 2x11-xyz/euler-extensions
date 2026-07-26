"""euler.causal_dag.v5 — schema, invariants, and walk import (milestone 1)."""

from .schema import Artifact, Node, Edge, SourceRef, Basis, Turn, Diagnostics, dumps, loads
from .invariants import Finding, CHECKS, check, recompute_diagnostics
from .structure import structural_projection
from .walk_import import import_walk
from .exports import to_dot, to_markdown, to_summary
from .viewer import viewer_payload, render_html, load_palette, VIEWER_SCHEMA, VIEWS

__all__ = [
    "Artifact", "Node", "Edge", "SourceRef", "Basis", "Turn", "Diagnostics",
    "dumps", "loads",
    "Finding", "CHECKS", "check", "recompute_diagnostics",
    "import_walk",
    "structural_projection",
    "to_dot", "to_markdown", "to_summary",
    "viewer_payload", "render_html", "load_palette", "VIEWER_SCHEMA", "VIEWS",
]
