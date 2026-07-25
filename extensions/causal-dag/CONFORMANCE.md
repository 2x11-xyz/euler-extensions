# CONFORMANCE — the v5 rule inventory and its justifications

This is the review standard for the causal-dag Python core (ruling 12,
DECISIONS.md, 2026-07-25): **v5 owes nothing to v3 parity.** Every rule the
validator enforces is listed here and justified by a v5 principle —
provenance grounding, honest degradation, deterministic serialization,
scientific-record integrity — or by a walk ruling (R1–R12). The archived
v3 suite (`spec/schema_conformance.rs`, 61 failure codes) was mined once,
completely; every one of its rules is dispositioned below as adopted,
adapted, deferred to the projection lane, or dropped with a reason. A
review finding of the form "v3 also checked X" is only actionable if X
serves a v5 principle this document missed.

## A. v5-native rules (no v3 antecedent)

| Rule | Check | Justification |
| --- | --- | --- |
| Per-kind status axes | `check_vocabulary` | Q2: v3's flat statuses miscoded refuted knowledge as `dead_end`; per-kind axes make decisive negative results expressible. |
| Rootness is topology, `roots()` derived | `Artifact.roots()`, `check_root_membership` | Q1: removes the root-kind conflation and a whole class of v3 root/kind contradictions structurally. |
| Every node owns ≥1 turn; every turn ≥1 event | `check_turns_nonempty` | R1: the turn is the atomic causal unit; an empty span cites nothing. |
| A turn and its events appear exactly once, anywhere | `check_one_node_per_turn` | R1/R2 strict segmentation, enforced within a node as well as across. |
| Turns sorted by step_id | `check_canonical_ordering` | Determinism extended to the turn model. |
| fork/decomposition never springs from a synthesis | `check_subgoal_forks_from_goal` | R9 mechanical shadow; the abandonment counterfactual stays in the review lane. |
| Verification never chains off a verification target | `check_verification_fans` | R10: verification fans; fixes ride repair/refinement edges. |
| Genealogy edges cite the failure's evidence | `check_generative_content_backed` | Q4: generative edges from content, never adjacency. |
| `genealogy_edge_count`, `refuted_claim_count` | `recompute_diagnostics` | Q4: the genealogy lens as a first-class metric. |
| Typed strict parse (closed keys AND value types, bool≠int, everywhere) | `schema._check` / `loads` | Determinism/integrity: nothing mistyped is silently accepted and rewritten. Strictly stronger than v3. |
| No NaN/Infinity; finite floats only | `_reject_constant`, `_finite`, `allow_nan=False` | Non-finite numbers have no JSON form; they break byte equality. |
| UTF-8 encodability (lone surrogates rejected) | `schema._utf8` | Round-trip byte stability. |
| Bounded nesting (32 levels) in opaque values | `_check_opaque`, `_canon` | Fail loudly instead of overflowing during dumps. |
| Serialized roots equal derived roots | `loads` | A derived field that disagrees with its derivation is a corrupt record. |
| dumps and loads share one validation path | `_validate_tree` | A scientific record never emits what it cannot re-read. |
| `check()` reports, never raises | engine contract | R7: the caller grades operator input; a raising validator cannot. |
| The event range tells the truth about the evidence | `check_range_honesty` | Scientific-record integrity: null range ⇒ empty artifact, null watermark, epoch generated_at; bounded range ⇒ watermark present. |

## B. Adopted / adapted from v3 (each re-justified)

| v3 rule (failure codes) | v5 check | Disposition |
| --- | --- | --- |
| schema/media-type identity | `check_schema_identity` | ADAPT — the self-identification check survives; the v3 string values are replaced by v5's. |
| extension_id / projection basis identity | `check_schema_identity` | ADOPT — self-identification. |
| construction enums + lineage pairing | `check_construction` | ADOPT — provenance grounding: lineage records derivation. |
| closed key sets (`unknown-field`/`required-field`) | `schema._check` | ADAPT — folded into the typed parse, stronger than v3. |
| `duplicate-id` | `check_id_uniqueness` | ADOPT — ids are the citation namespace. |
| `canonical-order` | `check_canonical_ordering`, `warning_sort_key` | ADOPT as *principle*; the order itself is v5-defined (Python tuple order), not v3's byte encodings. |
| node/edge/basis vocabulary enums | `check_vocabulary`, `check_source_ref_shape` | ADAPT — the checks survive; the node vocabulary is the walk's (Q1/Q3), the edge vocabulary re-affirmed by §3. |
| `source-ref-required` + `degraded-required` | `check_source_ref_shape` | ADOPT — basis kind dictates whether citations may be absent (honest degradation). |
| basis present; basis ids ⊆ local refs | `check_basis_required`, `check_source_ref_shape` | ADOPT — provenance is never optional. |
| source_ref variant shape; payload_pointer shape; blob name present | `check_source_ref_shape` | ADOPT (static halves) — a citation's shape must match its kind; resolution halves are deferred (C.1). |
| `metadata-shadow` | `check_metadata_shadow` | ADOPT — metadata must not override structure. |
| `edge-endpoint` | `check_edge_endpoints` | ADOPT — structural integrity. |
| backbone parent counts; root_id; reachability | `check_backbone_parent_count`, `check_root_membership`, `check_acyclicity` | ADAPT — reachability is subsumed: exactly-one-parent + acyclic + climb-to-root imply it (documented judgment call; no separate check by minimalism). |
| `annotation-backbone`/`backbone-class` | `check_backbone_class` | ADOPT — the backbone is the dependency spine. |
| cross-root rules | `check_cross_root` | ADOPT — R9/R11: cross-root reuse is annotation, never parentage. |
| cycle checks | `check_acyclicity` | ADOPT — re-implemented iteratively (terminates on any input). |
| `sequence-unmarked` + degraded warning coverage + incomplete-range⇒degraded | `check_degraded_marking` | ADOPT — honest degradation is a core v5 principle. |
| terminal-child ⇒ repair, sharing evidence | `check_terminal_children` | ADAPT — rule kept; the terminal set is redefined by the per-kind axes (refuted is terminal). |
| diagnostics recomputation + warning refs/severity/empty_forest | `check_diagnostics` | ADOPT — stored metrics must equal derived metrics. |

## C. Deferred to the projection lane (stream-bound, not dropped)

These rules' subject is the live event stream, which the static validator
deliberately does not see. They move lanes with the projector:
`source-event-missing` / `-kind` / `-range`; payload-pointer *resolution*
(with the degraded soft-fail); `opaque-reasoning-source`; artifact ref
kind/hash verification and `artifact-source-coverage`; blob sha256 match;
session-id-of-events; range endpoints exist and are ordered; watermark
exists; `generated_at` equals the end event's timestamp (the walk importer
already enforces this at production); `missing_source_ref_count` as a live
measurement (hardcoded 0 today, documented as inert).

Known semantic note: `source_backed_edge_count` is a *shape*-count in v5
(pointer well-formed) where v3 counted *resolved* pointers; the projection
lane may tighten it. Documented, deliberate.

## D. Dropped (no v5 principle demands them)

- **Backbone labels** (`backbone-label`, `-root`, and the five base-26
  algorithm tests): labels are a view materialization ("A.1.1"), not
  record content. Views derive them at render.
- **`occurrence-source-ref`**: superseded by the first-class `turns[]`
  model (R1). Open note: turns anchor *occurrence*, not necessarily
  *grading evidence*; if that distinction resurfaces it returns as a §2
  metadata rule, not a v3 inheritance.
- **v3 string constants** (schema id, media type): the identity check is
  kept; the values are v5's.
- **v3's warning-sort byte encoding** (NUL-joined JSON arrays): the
  *existence* of a canonical warning order is adopted; the order itself is
  v5-defined (`warning_sort_key`, Python tuple comparison).
- v3 error-message wording: messages are not rules.
