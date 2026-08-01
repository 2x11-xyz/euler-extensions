# Session Run Health

`session-run-health` is a removable first-party policy extension for evidence-
backed strategy checkpoints during long root runs. It owns no session loop,
provider deadline, queue, or background worker. Euler invokes its agent-only
`assess-run-health` command at the deterministic pre-request tick and supplies
one accepted durable-prefix cutoff. The extension reads and folds only through
that inclusive cutoff.

The extension stays quiet while work continues to produce substantive events.
It never caps total rounds or elapsed run duration. Provider inactivity and
cancellation remain Euler/provider-boundary responsibilities: an open root
model call suppresses the extension's no-progress symptom, and a provider
timeout becomes a completed failure observation rather than evidence of agent
deliberation.

## Symptoms

The default recurrence policy is explicit and session-configurable:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `failure_recurrence` | 3 | Same content-free tool/check/error class |
| `edit_recurrence` | 5 | Changes to one hashed file identity without a validation result |
| `no_progress_seconds` | 300 | Event-time gap without a substantive milestone |
| `context_window` | 4 | Complete root model-usage samples required |
| `minimum_context_tokens` | 8000 | Minimum latest input size for context churn |
| `context_growth_min_tokens` | 8000 | Minimum growth across the context window |
| `context_growth_percent` | 50 | Minimum relative growth across the window |
| `maximum_cache_reuse_percent` | 10 | Maximum reuse on every churn sample |
| `active_run_input_recurrence` | 1 | Delivered extra user messages before scope reassessment |

Context/cache churn fires only when the complete window is strictly growing,
crosses both growth thresholds, and every sample reports cache reuse at or
below the configured percentage. Missing optional cache telemetry is not
guessed. Samples are scoped to one hashed `(provider, model)` target and reset
on `model.switched` or any observed result-target disagreement, so usage from
different model targets can never manufacture one growth window. Only a
completed, recognized validation operation resets edit
recurrence. For `run_shell`, recognition is an anchored
executable/subcommand allowlist and completion requires an actual integer exit
status; incidental words such as `rg test src`, unrelated shell commands,
compound commands, cancellation, and recovery closure are not validation. A
completed recognized validation resets recurrence whether it passed or failed,
because either outcome is new evidence.

The reducer defers all health observations until durable root ownership is
established by `session.start` or, for a legacy stream, its first root
`run.started`. Pre-ownership history is discarded and child-agent events are
ignored after ownership is known. Provider/session errors participate in
recurrence only when they carry a non-empty structured `category`; unrelated
legacy errors without that content-free class remain visible in provenance but
are not merged into a synthetic `unspecified` failure class.

A durable extra `user.message` in an open run is observable evidence that
active-run instructions reached the next model boundary; it is not proof that
the text changed semantic scope. Euler atomically appends steering
`queue.delivered + user.message` before the following request tick, so the
extension counts the delivered message rather than pending queue admission.
Cancelled or replaced-before-delivery rows cannot trigger a checkpoint. Queue
admission, replacement, delivery, cancellation, and recovery bookkeeping is not
queried and does not reset the meaningful-progress clock.

## Checkpoint surfaces

When one or more thresholds cross, the extension writes one bounded JSON
artifact and publishes the same advisory through Euler's existing plan
presentation and `session-run-health` context slot. Evidence contains only
signal codes, counts, numeric usage, durable event IDs, thresholds, and
recovery options. It never contains transcript text, tool input/output/error
text, file paths, or model reasoning.

Checkpoint state is prepared atomically before host side effects. Artifact
metadata carries a deterministic signature computed only from the public
checkpoint document; hashed private entity identities and recency bookkeeping
cannot affect or disclose it. A restart reconciles matching artifact, plan,
and context events from the accepted feed. Capability-level `HostError`s caught
inside publication leave an exact pending effect for a later tick. If an error
escapes the command, Euler latches the contributor for that live session; a
fresh resume/process invocation owns reconciliation rather than an impossible
same-session retry.

A fully published checkpoint remains active until its evidence resolves or the
owning run terminates. A request-tick extension does not execute while the TUI
is merely idle, so terminal surfaces can remain displayed until the next root
request boundary. At that boundary the terminal is folded and retirement
finishes before the next canvas/provider request. Retirement is durable and
ordered for model safety: the extension first deletes its context slot with
empty content, then publishes the plan as `completed`. Each effect is
reconciled and exactly retryable after process loss, so stale model-facing
context cannot reach that next request. Durable alert markers prevent the same
unresolved symptom epoch from publishing on every request.

An escaping ordinary failure also appends an empty slot update when cleanup
succeeds. Active checkpoint state tracks that context publication separately.
On session resume, an unresolved active checkpoint that folds its own cleanup
tombstone republishes only the exact context advisory; its artifact and plan
are not duplicated. The slot effect precedes its durable acknowledgement, so a
crash between them is resolved by replay: a later cleanup tombstone causes one
more exact re-arm, while a failed cleanup leaves the published event to confirm
the state. If recovery evidence arrived in the same accepted prefix,
retirement wins and the stale advisory is never re-armed.

## Configuration

Defaults require no setup. To override them for one session, stop Euler and
create `run-health-config.json` alongside the extension's private state:

```text
<session-directory>/extensions/session-run-health/run-health-config.json
```

The document is a closed, partial override with `schema_version: 1`:

```json
{
  "schema_version": 1,
  "failure_recurrence": 4,
  "edit_recurrence": 7,
  "no_progress_seconds": 600
}
```

Unknown fields, booleans masquerading as integers, out-of-range values,
non-regular files, and corrupt JSON fail the extension tick closed. Euler's
request-tick host boundary remains fail-open and latches the failed extension
for the session, so a policy-extension failure cannot take over the root run.
Before any non-cancellation exception escapes—from load, query, fold, durable
store, or publication—the extension best-effort clears its fixed context slot.
No cleanup-side exception—including host, protocol, cancellation, I/O, or
runtime failure—can replace the original failure. The SDK's distinct
top-level `Cancelled` signal bypasses cleanup so ordinary host cancellation
neither destroys a valid advisory nor masquerades as a policy failure.
There are deliberately no environment-variable overrides or total-round/time
limits.

## Privacy and boundedness

The provenance query is kind-filtered and bounded to 8 returned events, 1024
scanned events, and 48 pages per tick. Every page repeats the exact host cutoff.
On a truncated page, its continuation must equal its watermark exactly; a
complete page must not carry a continuation. This prevents a malformed peer
from skipping accepted history.

Eight ordinary 8 KiB content-bearing events encode below 128 KiB in the
managed-process regression fixture, leaving substantial headroom below the
1 MiB message ceiling. This is deliberately conservative headroom, not an
absolute per-page byte guarantee: a single legacy inline event may have no
extension-enforceable size bound before the response frame is received. If
such an event alone exceeds the managed-process ceiling, the invocation fails
honestly; Euler's request-tick boundary fails open and latches this contributor
off, so the root run continues without a health advisory.

If that page budget is exhausted, durable state records and reports only the
cursor actually assessed, never the requested later cutoff; a subsequent tick
continues from that cursor. It does not publish a strategy checkpoint until the
fold reaches the exact cutoff, so a recovery later in the same accepted prefix
cannot be hidden behind the page boundary.

`model.reasoning` and runtime-only `model.delta` are excluded from the query.
`model.switched` is consumed only as a content-free usage-window boundary and
is never rendered. The current model target is retained only as a one-way
SHA-256 fingerprint in private state.
The fold needs event presence for a few content-bearing lifecycle/progress
kinds, but it never retains or publishes their text fields. Its only payload-
text classification is an in-memory allowlist check that recognizes common
validation invocations on `run_shell`, including ordinary environment prefixes;
the command is discarded immediately. File and failure identities are one-way
SHA-256 fingerprints in private state. Extension-owned artifact/plan/context
events are reconciled or ignored and never manufacture meaningful progress.

## Enable and test

The extension requires an Euler host that supports manifest `request_tick` and
the additive `ProvenanceQuery.through_event_id` bound:

```sh
euler extension validate extensions/session-run-health
euler extension link extensions/session-run-health
euler extension enable session-run-health
```

`extension enable` records launch consent only; it grants no capability. Before
the first root request in a session, open `/permissions`, choose **Advanced
capability settings**, and set exactly `provenance-read`, `extension-state`,
`artifact-write`, `context-slot`, and `plan-presentation` to `session-allow`.
Choosing the broader **Full access** posture also supplies authority, but is not
required by this extension. Existing user/project grants can satisfy a
capability only under the ordinary grant-aware permission mode.

Request ticks are implicit and cannot interrupt a model request with a
permission prompt. If any required standing authority is missing, Euler fails
open and latches this contributor off for that live session; correct
`/permissions` and start or resume a new session before expecting checkpoints.
No environment-variable grant or hidden startup ritual is supported.

From this repository's root:

```sh
python -m unittest discover -s extensions/session-run-health/python/tests -v
```

Reducer fixtures derived from 8KQ and 4F1 cover the provider-stall boundary and
failure/edit/context symptoms before round 104; they are minimized behavioral
fixtures, not canonical session logs. Tests also cover a canonical-shaped
many-round event sequence, productive long work, repeated malformed patches,
missing cache telemetry, cutoff pagination, process death after every host
surface effect, and durable retirement.
