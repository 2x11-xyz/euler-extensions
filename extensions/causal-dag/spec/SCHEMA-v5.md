# euler.causal_dag.v5 — draft schema from the annotation walk

Status: **draft for review**. This is the target schema for the Python
rewrite's projection. Unlike v3 (whose fixtures and conformance suite pinned
artifact *shape*), v5 encodes projection *behavior*: it was derived from a
human annotation walk over a real session (Knuth up-arrow, 743 events), where
the intended causality was drawn by hand, turn by turn, and every modeling
dispute was settled explicitly. Those rulings live in the annotation tool's
`DECISIONS.md` (2x11-xyz/causal-dag-annotation-tool); each normative clause
below cites its ruling (R1–R11) or open question (Q1–Q4).

v3 and v4 are read as prior art throughout: v5 keeps v3's artifact envelope
and lineage machinery (proven infrastructure) and adopts v4's central insight
(per-kind status axes) with the walk as evidence.

## 1. Design principles

1. **Provenance-grounded.** Every node and edge cites provenance events
   (`source_refs`); the graph asserts nothing it cannot cite. Payload
   pointers cite *regions* of a turn as evidence — they never define node
   boundaries (R1).
2. **The turn is the atomic unit of causality** (R1). A node owns one or
   more whole turns; a turn belongs to at most one node (R2). Two
   experiments performed in one turn are one node.
3. **Two causal lenses over one artifact** (Q4). The backbone encodes
   *dependency* (what each result rests on). *Genealogy* (why each attempt
   happened) rides in the `{repair, pivot, refutation}` edge family.
   Views select a lens; the artifact stores both. A generative edge is
   drawn only where content shows dependence — never from adjacency.
4. **Honest degradation.** Loss of fidelity is always declared, on the axis
   where it occurs. *Structural* degradation (chronology fallback, an
   incomplete window) is declared via `projection.degraded`, which also
   relaxes the citation rules below. *Vocabulary* loss (a status mapped
   lossily during import) is declared via `lossy_status_mapping` warnings
   naming the affected nodes — it does not set `projection.degraded`,
   because the graph's structure and citations are intact and relaxing
   structural rules for a vocabulary approximation would make the artifact
   *less* honest. Both channels are explicit declarations; neither loss is
   ever silent.
5. **Statuses live.** Nodes are born `open` and change status by evidence,
   not birth (R3). A projection whose nodes are born terminal is failing
   (the old system's frozen-status pathology).

## 2. Node model

### 2.1 Kinds

| Kind | Definition | Provenance from walk |
| --- | --- | --- |
| `question` | A goal or question the work serves; questions decompose into sub-questions. | Q1: the "last-digits fast path" subgoal had no kind to live as. Adopted from v4. |
| `claim` | An assertion evidence can support or refute. A hypothesis is a claim whose verdict is open (R4). | Attempt 1's refuted closed-form. |
| `investigation` | A bounded effort undertaken to answer a question or assess a claim: an experiment, implementation, derivation, search, study. | The walk's "attempts". Cross-domain rename per Q3; `attempt` remains a sanctioned display label. |
| `synthesis` | A node that consolidates or integrates prior work into a stated result. Mechanical test: a *consolidating* synthesis only references existing results (the walk's ledger write-ups); an *integrating* synthesis produces a new combined artifact/result (the final knuth.py). Both are `synthesis`; the distinction is the `consolidation` metadata flag. | Q3 resolution: `checkpoint` is dropped as a kind — the checkpoint/synthesis choice was not reliably codable; the flag makes it mechanical. |

**Rootness is topology, not a kind** (Q1). A root is any node with no
backbone parent; the `root` kind is removed. Top-level nodes are normally
`question`s. Viewers keep the always-gold treatment for roots (R5).

Non-goals for now: v4's `observation` and `artifact` kinds are not adopted —
in-session, provenance *is* the observation layer and `extension.artifact`
events are citable directly via source_refs. Revisit if walks over
literature-based research (external evidence) demand it.

### 2.2 Status: per-kind axes (Q2, adopting v4's split)

One `status` field per node; the legal value set depends on kind.

- `question`: `open`, `blocked`, `answered`, `abandoned`, `superseded`
- `claim` (epistemic verdicts): `open`, `supported`, `refuted`, `proven`,
  `inconclusive`, `superseded`, `abandoned`
  - `refuted` is a *result*, not a failure: the walk's most consequential
    event (the falsified closed-form) was inexpressible in v3's vocabulary
    and had to be miscoded `dead_end` (Q2).
  - `proven` is reserved for deductive domains; empirical domains top out
    at `supported` (with accumulating evidence edges).
- `investigation` (work outcomes): `open`, `blocked`, `dead_end`,
  `succeeded`, `verified`, `superseded`, `abandoned`
  - `succeeded` = it worked; `verified` = independently confirmed (a
    verification-kind edge from this node's artifact/result exists). The
    walk graded these distinctly and the distinction carried information.
- `synthesis`: `open`, `stated`, `verified`, `superseded`, `abandoned`

Terminal statuses (for the structural rule in §4): `dead_end`, `refuted`,
`superseded`, `abandoned`, `blocked`. Note `refuted` is terminal for the
*claim* while being a success for the *session* — the genealogy lens (§3)
is where its productive role is visible.

Palette: the v3 eight-status palette tokens map onto v5 statuses
(`answered`/`succeeded`→success, `supported`/`stated`→success family,
`proven`/`verified`→verified, `refuted`→dead_end family with the refutation
glyph) so existing viewer tokens survive; exact token table to be fixed in
the palette asset when the viewer is re-integrated.

## 3. Edge model

Unchanged from v3 in class/kind vocabulary — the walk validated it
("refutation, pivot, supersedes, repair, fork all earned their keep"):

- `structural` (backbone-eligible): `continuation`, `refinement`, `repair`,
  `fork`, `decomposition`, `integration`, `verification`
- `annotation`: `evidence`, `refutation`, `artifact_use`, `pivot`,
  `related`, `supersedes`
- `chronology`: `sequence` (degraded projections only; forbidden in
  semantic hints)

**Influence direction** (R14): every directed edge points in the direction
of influence — the `from` node acts on, feeds, or changes the standing of
the `to` node. The backbone already conforms (a parent's work enables its
children). Among annotations, `artifact_use` runs producer → consumer (its
product feeds the target, not a citation back to it); `related` is symmetric,
so its direction carries no meaning. Time-backward arrows are legal and
meaningful: a later refutation influences the standing of an earlier claim.

Backbone rule (unchanged): every non-root node has exactly one incoming
`canonical_backbone` structural edge; backbone edges never cross roots and
never cycle.

**Genealogy lens** (Q4): the ordered edge family `{repair, pivot,
refutation}` reconstructs the search trajectory. Self-test for projection
completeness: for any terminal node, "what would disappear had this failure
not happened?" must be reachable via its outgoing pivot/repair/refutation
edges.

## 4. Structural invariants (the rulings)

Checkability varies and the checker must be honest about it: rules 1, 2, 6
and 9 are fully machine-checkable; rules 3 and 4 have mechanical *shadows*
(fork/decomposition never springs from a synthesis; verification never
chains off a verification target) while their full intent — the
abandonment counterfactual — stays in the review lane; rules 5, 7 and 8
are intent/lifecycle rules a static snapshot cannot check at all.

1. One node per turn; nodes may span non-contiguous turns (R1, R2).
2. Terminal-status nodes take structural children only via `repair` edges,
   and a repair must share evidence with the failure it repairs (v3 rule,
   revalidated in the walk).
3. Subgoals and competing approaches fork from the goal/question they
   serve, never from a sibling branch's synthesis; cross-branch reuse is
   `artifact_use`/`evidence`, never parentage (R9). Test: "would this work
   still make sense if the other branch were abandoned?"
4. Verification fans: independent verifications attach in parallel to the
   node they verify via `verification` edges; chains appear only where a
   verification failure spawns a fix (R10).
5. Integration attaches to the goal it delivers, drawing from contributing
   branches via annotations (R11).
6. Generative edges (`repair`, `pivot`, `refutation`) must be
   content-backed: the **edge's** source_refs overlap the failure (from)
   node's evidence or cite its counterexample (Q4 discipline). Adjacency
   alone never justifies an edge. The evidence lives on the edge, not on
   the citing node — under turn atomicity (R1) two nodes own disjoint
   turns, so node-to-node event overlap is structurally impossible; this
   matches how v3's conformance suite enforced the terminal-repair rule.
   The same edge-carries-evidence reading applies to the repair rule in
   invariant 2.
7. Nodes are created `open` (R3).
8. Statuses are graded on the node — the set of turns — never per turn (R6).
9. Backbone integrity violations in *operator/annotation* inputs are
   reported, not rejected (R7); violations in *projection output* are
   defects.

## 5. Artifact envelope

v5 inherits the v3 envelope wholesale — `schema`, `media_type`,
`generated_at`, `session{id, event_range}`, `projection{extension_id,
watermark_event_id, basis, degraded}`, `construction{operation, policy,
trigger, predecessor_*, observer_result_event_id}`, `forest{roots,
active_root, nodes, edges}`, `diagnostics` — with:

- `schema: "euler.causal_dag.v5"`,
  `media_type: "application/vnd.euler.causal-dag.v5+json"`.
- `forest.roots` derived (nodes with no backbone parent), not asserted.
- Node shape: `id, kind, status, title, summary, turns[], source_refs,
  basis, metadata`. `turns[]` is new: the ordered event-id spans of the
  turns the node owns (R1) — segmentation is first-class, not recoverable
  only from source_refs. `root_id` is retained (derived) for viewer compat.
- Edge source_refs: every edge carries its own citations. For
  operator-asserted graphs (walk imports) where the human drew edges
  without explicit citations, the converter derives one `event` ref
  anchored on the from-node's evidence — which is what makes repair/
  pivot/refutation edges share evidence with the failure they spring
  from by construction (§4.2, §4.6). Observer-asserted edges must cite
  their evidence explicitly. **Degraded exception:** in a projection
  marked `degraded`, nodes and edges whose basis is `inferred` or
  `chronology` may carry no source_refs — the chronology fallback asserts
  ordering, not causality, and pretending citations it does not have
  would be dishonest. A degraded projector should still cite where it
  can (its nodes own turns whose events are known).
- Diagnostics: v3's recomputed counter set carries over where meaningful;
  counters referring to removed concepts (`root_count` by kind) re-derive
  from topology. Two additions: `refuted_claim_count` and
  `genealogy_edge_count` (the Q4 lens is a first-class metric).
- Lineage rules (predecessor chaining, immutable revisions, active
  pointer) carry over from v3 unchanged.

## 6. Compatibility and conformance (ruling 12)

v5 owes nothing to v3 parity. The implementation is measured against this
spec, the walk rulings (DECISIONS.md), and the gold-standard walk data —
never against the archived v3 suite, which is a source of ideas adopted
deliberately, one by one, each justified by a v5 principle (provenance
grounding, honest degradation, deterministic serialization,
scientific-record integrity). The adopted set and the reasons live in
`CONFORMANCE.md`, which is the review standard.

- **Viewer:** the archived HTML viewers (2D top-down, indented spine, 3D
  and 3.5D constellations, recoverable from euler git history) are adapted
  to render v5 natively — the v5 kinds and per-kind statuses get their own
  visual identity. There is no v3 down-conversion lane.
- **Canonical serialization is v5-defined:** deterministic byte-identical
  output with closed key sets, id-sorted collections, canonically ordered
  warnings (code, severity rank, message, id tuples — Python tuple order),
  and shared validation between reading and writing. v3's encodings are
  not authoritative.
- **Hints:** the observer contract becomes `euler.causal_dag.hints.v3`,
  identical in shape to hints.v2 with the v5 kind/status vocabulary and
  `turns[]` on nodes. The backbone rule, source_ref shape, basis kinds, and
  occurrence anchors carry over deliberately (provenance grounding).
- **Acceptance gate:** (a) the invariant checker returns no findings and
  (b) the walk's gold graph round-trips: projecting the baseline session's
  annotation export must satisfy every invariant and reproduce the human
  graph's segmentation and backbone shape.

## 7. Open decision points for review

1. `investigation` vs keeping `attempt` as the canonical kind name
   (display-label compromise proposed in §2.1).
2. The exact `synthesis` status axis (§2.2 proposes `stated`; alternatives:
   reuse `succeeded`).
3. Whether `blocked` is terminal (§2.2 lists it; unlike v3, a blocked node
   arguably should accept non-repair children once unblocked — proposal:
   `blocked` is terminal *while current*, and unblocking is a status
   change back to `open`).
4. ~~Whether the v3 down-converter is a launch requirement or a follow-up.~~
   Resolved by ruling 12: no down-converter — the viewers adapt to v5.
5. Schema id: `v5` continues the lineage past v4; alternative is a fresh
   identifier line if v5 is considered a different artifact family.
