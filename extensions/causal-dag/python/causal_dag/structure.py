"""The structural projection: SCHEMA-v5 §6's "segmentation and backbone shape".

A content-free view of an artifact — node identity/kind/status with exact turn
ownership, and every edge's endpoints/class/kind/backbone — used by the gold
acceptance gate to verify the graph's *shape*, not just its counts. Titles,
summaries, notes, and bases are deliberately excluded so the projection of the
gold graph can live in the public repository.
"""

from __future__ import annotations

from typing import Any, Dict

from .schema import Artifact


def structural_projection(artifact: Artifact) -> Dict[str, Any]:
    return {
        "session_id": artifact.session.id,
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "status": n.status,
                "turns": [{"step_id": t.step_id, "event_ids": list(t.event_ids)}
                          for t in n.turns],
            }
            for n in sorted(artifact.nodes, key=lambda n: n.id)
        ],
        "edges": [
            {
                "id": e.id,
                "from": e.from_node,
                "to": e.to_node,
                "class": e.edge_class,
                "kind": e.kind,
                "backbone": e.canonical_backbone,
            }
            for e in sorted(artifact.edges, key=lambda e: e.id)
        ],
    }
