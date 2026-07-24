# causal_dag (Python) — milestone 1

The reference Python implementation of the `euler.causal_dag.v5` artifact. Stdlib
only, Python >= 3.9, zero dependencies. `spec/SCHEMA-v5.md` is the contract.

## What exists

Milestone 1 is the model and its acceptance gate — no commands, manifest, or SDK
wiring yet, and no fold engine for hints/observer (those arrive with later
phases and are deliberately not stubbed).

- `causal_dag/schema.py` — dataclasses for the v5 artifact (envelope, `Node`
  with `turns[]`, `Edge`, `SourceRef`, `Basis`, `Construction`, `Projection`,
  `Diagnostics`), the §2-3 vocabularies (per-kind status axes), and canonical
  `dumps`/`loads`: closed key sets in fixed order, lists sorted by id,
  deterministic bytes.
- `causal_dag/invariants.py` — the §4 structural invariants as a data-driven
  table: one small named check per invariant, an engine (`check`) that runs
  them, and `recompute_diagnostics` (the single source of truth for §5's
  counters). Checks read the artifact only, never a live event stream. Per
  R7/§4.9 the engine reports (returns findings) and never raises — the caller
  decides whether an operator-input finding is advisory or, in projection
  output, a defect.
- `causal_dag/walk_import.py` — converts a `causal-dag.walk-annotations.v2`
  export plus its session steps into a v5 artifact. The old→v5 kind and
  per-kind status mapping tables, and the handling of the deliberately-unplaced
  node, are documented in the module docstring.

## Invariants (§4 + inherited v3 envelope rules)

| Check | Guards |
| --- | --- |
| `id_uniqueness` | node/edge ids disjoint; source_ref ids globally unique |
| `canonical_ordering` | roots, nodes, edges, source_refs, basis ids sorted and unique |
| `vocabulary` | node kind + per-kind status axis (§2); edge class/kind (§3) |
| `source_ref_shape` | basis kind, variant fields, pointers, basis→source_ref coverage |
| `edge_endpoints` | every edge names existing nodes |
| `backbone_parent_count` | roots have 0 backbone parents, others exactly 1 |
| `root_membership` | `root_id` matches the backbone root; `active_root` is a root |
| `cross_root` | backbone edges stay intra-root; cross-root edges are annotations |
| `acyclicity` | structural, backbone, chronology subgraphs are acyclic |
| `terminal_children` | terminal nodes take only repair children, sharing evidence (§4.2) |
| `subgoal_forks_from_goal` | fork/decomposition never springs from a synthesis (§4.3 shadow) |
| `verification_fans` | verification never chains off a verification target (§4.4 shadow) |
| `generative_content_backed` | repair/pivot/refutation edges cite the failure (§4.6) |
| `one_node_per_turn` | a turn and its events appear exactly once, anywhere (R1, R2) |
| `diagnostics` | counters match a fresh recomputation; warnings resolve (§5) |

Plus the envelope checks: `schema_identity`, `construction`, `backbone_class`,
`degraded_marking`, `basis_required`, `metadata_shadow`, `turns_nonempty`.
§4 rules 5, 7 and 8 (integration-to-goal intent, born-open, node-level grading)
are intent/lifecycle rules a static snapshot cannot check — they live in the
review lane, not this table.

## LOC budget (a stated contract)

- core (`schema.py` + `invariants.py` + `walk_import.py`) ≤ ~1,400 non-blank
  lines — revised up from the original ~1,200 after five review rounds of
  envelope hardening (typed strict parsing, iterative traversals, importer
  honesty); the additions are missing checks, not padding, and raising the
  line honestly beats gaming it;
- Python extension code (this directory, excluding tests) ≤ ~4,000 as later
  milestones land; the archived Rust prior art under `../spec/` is reference
  material and does not count;
- tests ≤ logic.

Measure with `grep -cve '^\s*$' causal_dag/*.py` (currently: core 1,260,
tests 514).

## Running the tests

```
python3 -m unittest discover -s extensions/causal-dag/python/tests
```

`test_invariants.py` is a table-driven check over a hand-written 6-node fixture:
it asserts the fixture passes every invariant and that each invariant fires on a
targeted mutation. `test_gold_roundtrip.py` projects the annotation walk's gold
export (loaded from the annotation tool's local checkout, with `walk.db` for the
turn→event mapping) and asserts conversion passes all invariants, counts match
the export, and serialization is deterministic. It skips cleanly when the gold
data is absent — that data is kept out of this public repo.

## Acceptance bar (SCHEMA-v5 §6)

An artifact is accepted when (a) the §4 invariant checker returns no findings and
(b) the walk's gold graph round-trips: projecting the baseline session's export
satisfies every invariant and reproduces the human graph's segmentation and
backbone shape.
