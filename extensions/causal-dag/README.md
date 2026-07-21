# causal-dag (spec only — implementation pending redesign)

Status: **paused for behavior redesign**. The bundled Rust implementation was
deliberately not ported when euler went core-only: its integration worked, but
its behavior was not yet what we want, and porting ~13k lines of logic would
have frozen the wrong behavior into a second codebase. The next implementation
is planned in Python over the managed-process protocol (language choice per the
first-class-citizen principle; measured overhead vs in-process Rust is ~40 ms
per invocation, dominated by interpreter startup).

This package preserves the golden rails any rewrite must satisfy:

- `spec/fixtures/` — three fixture cases (`knuth_style_search`,
  `code_review_study`, `emdash_mechanism_analysis`), each pairing canonical
  session events with the expected `euler.causal_dag.v3` artifact.
- `spec/schema_conformance.rs` — the executable acceptance rules, copied
  verbatim from the removed crate's test suite.
- `spec/SCHEMAS.md` — the full schema-identifier inventory.
- `docs/causal-dag.md` — the human-facing guide from the euler repo.

Known constraint for a managed-process implementation: the host caps requests
at 64 per invocation; the old `catch-up` tick loop exceeded that (~81), so the
rewrite should make catch-up resumable across invocations with a continue
cursor.

The redesign method (agreed 2026-07): walk a real session's snapshot series
against its transcript, narrate the intended causality at each divergence, and
turn that narration into the behavior spec — behavior first, then schemas and
fixtures pressure-test it.
