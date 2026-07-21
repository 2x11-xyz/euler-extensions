# causal-dag artifact schemas

Schema identifiers the bundled Rust implementation emits and accepts, extracted
from `euler-extension-causal-dag` at its point of removal from the euler repo
(euler @ 08ef7a1, 2026-07-21). Any rewrite must produce artifacts that validate
against these names and the fixture set in `fixtures/`.

## Primary artifacts

| Schema | Purpose | Source of truth |
| --- | --- | --- |
| `euler.causal_dag.v3` | Main DAG artifact (fixtures validate this) | `src/lib.rs` `SCHEMA_NAME` |
| `euler.causal_dag.v4` | Research-mode DAG (`RESEARCH_DAG_SCHEMA`) | `src/research_record.rs` |
| `euler.causal_dag.hints.v2` | Observer hints folded by observer-apply | `src/lib.rs` `HINTS_SCHEMA_NAME` |
| `euler.causal_dag.observer_brief.v1` | Brief handed to the companion model | `src/lib.rs` |
| `euler.causal_dag.observation_record.v1` | Durable observation record | `src/record_observation.rs` |

Media types: `application/vnd.euler.causal-dag.v3+json` (current),
`v2+json` (prior), `v1+json` (legacy read-compat), `v4+json` (research mode).

## Supporting schemas

| Schema | Purpose |
| --- | --- |
| `euler.causal_dag.active.v3` | Active-state projection (`src/active_state.rs`) |
| `euler.causal_dag.export.v1` | Export metadata (`src/export.rs`) |
| `euler.causal_dag.view.v1` | View payload (`src/view.rs`) |
| `euler.causal_dag.viewer.v2` | HTML viewer graph payload (`src/export/graph.rs`) |
| `euler.causal_dag.palette.v1` | Export palette (`src/export/palette.rs`) |

## Executable spec

`schema_conformance.rs` (copied verbatim from the crate's tests) encodes the
acceptance rules — root/edge/provenance shape — against canonical events. It
deliberately imports `euler_event` rather than the projector so schema validity
stays independent of any implementation. The three fixture cases each pair
`events.jsonl` (canonical session events) with `expected.causal-dag.json`
(the byte-exact expected v3 artifact, modulo projection metadata).
