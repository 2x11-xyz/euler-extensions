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

## Milestone 2 — exports and the native v5 viewers

The renderers and viewer projection that turn a v5 `Artifact` into shareable
output. All pure and deterministic; the viewers adapt the archived HTML shells
to v5 natively (ruling 12 — no v3 down-conversion).

- `causal_dag/exports.py` — three renderers over an `Artifact`:
  - `to_dot(artifact)` — a Graphviz digraph: solid backbone edges, dashed
    annotation arcs labelled by kind, node colour/glyph by v5 status (roots
    gold, R5).
  - `to_markdown(artifact)` — per-root backbone outline, then **Dead ends**,
    **Decided (refuted)** (a refuted claim is knowledge, so it gets its own
    heading — never the dead-end pile), **Open frontier**, and **Cross-arcs**.
  - `to_summary(artifact, budget=4096)` — the context-slot text
    (`GRAPH: … / DEAD ENDS / ACTIVE PATH / OPEN`), fit to budget by dropping
    OPEN first, then trimming ACTIVE PATH from the front, then shortening
    dead-end reasons, then dropping dead ends — dead ends survive longest.
- `causal_dag/viewer.py` — `viewer_payload(artifact)` (schema
  `euler.causal_dag.viewer.v5`): folds each node's single backbone parent in,
  assigns a parent-before-child `sequence` by a backbone walk from the
  active/first root (ties break on chronological `occurrence` — the node's
  first-turn step, the axis the 3.5D view rides), and turns non-backbone edges
  into cross-arcs. `render_html(artifact, view)` assembles one self-contained
  page per view (`top-down`, `indented`, `3d`, `3-5d`) by inlining the shells,
  `runtime.js`, the bundled React UMD builds, and `viewer/palette-v5.json`.
- `viewer/palette-v5.json` — the v5 palette (next to the untouched v3
  `palette.json`): the carried-over eight status tokens plus `succeeded`/
  `answered` (success family), `verified`/`proven` (blue, distinct glyphs
  `✓`/`✔`), `supported` (supportive mid-green `⊕`), `stated` (quiet `◇`), and
  `refuted` — a first-class decisive treatment in crimson `#D7263D` with the
  logical-falsum glyph `⊥`, deliberately **not** the dead-end vermillion. Kinds
  (+ consolidation) carry 2D-detail glyphs; annotation arc colours carry over.
- The four HTML shells were adapted minimally (`runtime.js` is unchanged): the
  always-gold root treatment now keys off payload `isRoot` (rootness is
  topology in v5), and the 2D detail cards show the kind glyph.

### Running the renderer

```
python3 tools/render_gold.py --out /tmp/m2-out
```

Projects the private gold walk (and the public mini fixture), asserts the
invariant checker is finding-free, and writes `artifact.json`, `gold.dot`,
`gold.md`, `gold.txt`, and `gold-*.html` / `mini-*.html`. Each page is
self-contained (no external URLs) and embeds a re-parseable v5 payload.

## Milestone 3 — the runnable managed-process extension

The package becomes a real Euler extension: a `managed-process` runtime that the
host launches (`python3 python/extension.py`), speaking the newline-delimited
JSON-RPC protocol through the vendored SDK under `_sdk/` (a verbatim mirror of
`euler`'s `euler_managed_process_sdk`, pending its publication — see that
directory's README). `extension.py` only bootstraps `sys.path` for the SDK and
the package, then hands three commands to `serve`.

### The projector (`causal_dag/projection.py`)

Honest degraded scaffolding, not analysis. Without the observer lane
(milestone 4) nothing can judge which turns are epistemic and which are clerical
(R13), so the projector does the one thing page order supports truthfully:

- **Filter** — a declarative `INCLUDE_KINDS` frozenset (the ownable surface:
  `user.message`, `assistant.message`, `model.result`, `tool.call`,
  `tool.result`, `patch.proposed`, `file.diff`, `check.result`, and `error` —
  failures are epistemic, the walk extractor owned error steps) plus the
  `is_owned` predicate. `model.call` is queried as a turn *boundary* but never
  owned; permission/canvas events, `agent.spawn` / `agent.result` observer
  bookkeeping (R13), and our own `extension.artifact` / `context.slot.updated`
  self-events are excluded (the feedback loop the host filter already
  forecloses, since `QUERY_KINDS` never requests them). One self-event channel
  a kind filter cannot catch: the host appends an `error` event when an
  extension command fails, so `is_self_error` drops *this extension's own*
  failure records before segmentation — a failing tick must not pollute its
  own graph — while genuinely epistemic session errors stay owned.
- **Segment** — `group_turns` cuts the page into turns (R1): the ownable span
  from one `model.call` to the next; a `user.message` opens its own turn. Empty
  spans (bookkeeping between boundaries) are dropped — clerical turns stay
  unowned (R13). Segmentation is **pagination-invariant**: the turn a page
  leaves open is carried across ticks in `active.json`'s `pending`, and
  `split_closed` prepends it before grouping. Only turns closed by a later
  boundary are projected; the still-open trailing turn is held back. The
  projected node structure is therefore identical for any page `limit` — where
  a page happens to truncate never changes the graph.
- **Project** — each turn becomes one open `investigation` node (the first turn
  of a session that opens with the user is the `question`), laid on a single
  chronology `sequence` spine with `canonical_backbone` edges and `chronology`
  bases. Nodes cite their turns' events where known. The whole result is
  `projection.degraded` with a `degraded_chronology` warning over **every**
  sequence edge — the disclaimer that these edges assert ordering, not
  causality. Every artifact runs through `check()` before it is written; a
  finding is an error, never a silent write.

### Commands (`causal_dag/commands.py`)

| Command | Capabilities | What it does |
| --- | --- | --- |
| `update` | provenance-read, artifact-write, fs-read, fs-write, context-slot | One checkpointed tick: load `main` checkpoint → query the next page → extend the artifact (predecessor = prior active artifact) → `check()` → `write_artifact` → replace `active.json` (atomic temp+rename) → `update_context_slot("graph", to_summary(…, 4096))` → store checkpoint. Empty/self-only pages short-circuit but still advance the cursor. |
| `catch-up` | (same) | Loops `update` ticks under a **strict host-request budget** — a wrapper counts every host call and hard-stops before 58 of the 64-per-invocation cap (≈6/tick + slack). When it cannot finish it returns `{caught_up: false, continue: true, …}`; the checkpoint is already durable, so the caller re-invokes and resumes. This is the resumable-cursor design that fixes the old 81 > 64 over-budget failure. |
| `export` | fs-write, artifact-write | Reads the cached `active.json` from the state dir and renders the active artifact to `json` \| `dot` \| `markdown` \| `summary` \| `html` (`view` selects one of the four viewer shells; the page is nav-free and self-contained). Errors politely (`run update first`) when there is no active artifact. |

The active projection state lives in the **session-scoped** extension private
state dir (`state_dir()`), so distinct sessions never collide and the git tree
stays clean. `active.json` is a small pointer — artifact event id, sha256,
relative path, watermark, a cached copy of the artifact JSON so an incremental
tick extends the prior revision without re-reading all of provenance,
`pending` (the owned events of the open turn carried across ticks), and
`cursor` (the page cursor the state is synced to). Reading and writing it is
the process's own I/O, not a host API.

#### Degradation, self-heal, and the size ceiling

The degraded spine is built to *diagnose* its limits, never to wedge:

- **No replay.** Every tick writes `active.json` (with its `cursor`) *before*
  advancing the host checkpoint, so after a crash between the two the state's
  cursor is ahead and authoritative — the next tick queries from it and no
  already-segmented event ever re-enters the projector (a replayed boundary
  would cut the open `pending` turn at a stale position). The host checkpoint
  is the durable fallback used only when the state file itself is lost.
- **State self-heal.** A missing *or corrupt* `active.json` is treated as
  absent. If the checkpoint cursor is still set (the pointer was lost
  mid-stream), the tick ignores the cursor, re-queries from the beginning, and
  rebuilds the entire graph as an honest fresh `snapshot` — the result carries
  `recovered: "state-rebuilt"` with `state_was: "corrupt" | "missing"`. A
  mid-stream fragment is never labelled a snapshot. (`export` over corrupt state
  raises a clear `ValueError`.)
- **Byte budgets — an honest ceiling, not a `ProtocolError`.** Artifact bytes
  are framed for the wire as base64 (×4/3) plus ~1KB of envelope. A single
  write past ~900 KB (below the 1 MiB message cap) is *not* written and the
  checkpoint is *not* advanced; the tick returns a structured halt
  `{ "projected": false, "halted": "artifact-size-limit", "artifact_bytes": N,
  … }`. `catch-up` additionally stops before a tick whose write could push
  cumulative output past ~3 MB (below the 4 MiB output cap), returning
  `{ "continue": true, "stopped": "byte-budget", … }`. When the degraded spine
  reaches this ceiling the observer lane (milestone 4) supersedes it — the
  degraded pass diagnoses the stop rather than crashing on it.

> **Capability note — `export` needs `fs-write`, not `fs-read`.** The task drafted
> `export` as `(fs-read, artifact-write)`, but the host gates `state_dir()` on
> `fs-write` (`ExtensionHost::state_dir` → `require_capability(FsWrite)`), and
> locating the private state dir is the only way to recover the active artifact
> (checkpoints hold only a cursor; there is no `fs-read` host API that returns
> artifact bytes). So `export`'s minimal *working* set is `fs-write` +
> `artifact-write`. This is verified against the real binary — the run banner
> announces exactly `fs-write, artifact-write`.

### Link → enable → run walkthrough

Linked (`path:`) extensions run unpinned working-tree code; the lightweight
grant is launch consent — `enable` echoes the exact argv once.

```
EULER=/path/to/euler/target/release/euler
export EULER_HOME=/tmp/euler-home            # isolate the registry; never touch ~/.euler

$EULER extension validate extensions/causal-dag        # manifest parses
$EULER extension link     extensions/causal-dag        # status: needs-review
$EULER extension enable   causal-dag                   # echoes ["python3","python/extension.py"]

# Linked runs operate on a session log directly (session id, name, or events path)
# and take the command input as a JSON object file:
cp some/events.jsonl /tmp/s/events.jsonl
printf '{"limit":64}'                > /tmp/s/update.json
printf '{"max_ticks":8,"limit":64}'  > /tmp/s/catchup.json
printf '{"format":"summary"}'        > /tmp/s/export.json

$EULER extension run causal-dag.update   /tmp/s/events.jsonl --input-file /tmp/s/update.json
$EULER extension run causal-dag.catch-up /tmp/s/events.jsonl --input-file /tmp/s/catchup.json
$EULER extension run causal-dag.export   /tmp/s/events.jsonl --input-file /tmp/s/export.json
```

Each invocation runs exactly one command against the accepted durable prefix of
the log. Artifacts, checkpoints, and `active.json` are written under
`<log-dir>/extensions/causal-dag/`. The 64-request-per-invocation cap is why
`catch-up` accounts strictly and resumes rather than blowing the budget: a
verified run reports `requests_used` well under 64 (≈6 per writing tick).

### Tests

`tests/test_extension.py` drives the handlers over an in-memory `FakeHost`
(bounded cursor-paged feed, checkpoint/slot/artifact stores, a request counter):
a first tick yields a `check()`-clean degraded artifact with a ≤4096-byte slot
and an advanced checkpoint; a second tick with no new events short-circuits; an
incremental tick chains predecessor lineage; `catch-up` stays under 64 requests
and returns resumable continue-state; every export format renders; and the
turn-grouping filter honors boundaries and excludes self/permission events. No
live host is involved.

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
