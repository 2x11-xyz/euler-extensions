# Plan / Todo

`plan-todo` is an extension-owned planning workflow for long-running Euler
tasks. It contributes the `update_plan` model tool and one terminal-idle
decision. There is no `/goal` command: for nontrivial work the model can select
the tool directly, just as it selects any other advertised tool.

Each `update_plan` call replaces the whole plan. A plan has 1–16 ordered items,
uses only `pending`, `in_progress`, and `completed` item statuses, and permits
at most one `in_progress` item. Its separate `plan_status` is one of:

- `active`: unfinished work is actionable, so terminal idle asks Euler to
  continue;
- `blocked`: no work can proceed without an external change, so idle stops;
- `waiting`: continuing now would only poll, so idle stops.

`blocked` and `waiting` updates require a concise explanation. A plan whose
items are all complete always stops, regardless of `plan_status`.

## State and context

The full plan is validated and atomically replaced in Euler's private,
session-scoped extension state directory under the narrow `extension-state`
capability. It never requests workspace file authority. The JSON file is the
source of truth. Writes flush the file before rename and then sync the
containing directory. Corrupt or unknown state is rejected instead of being
silently reset, and its stale context projection is cleared.

The extension also maintains a bounded `plan` context slot containing progress,
the explicit workflow state, and compact item lines. That slot is a best-effort
projection: durable state still drives idle continuation if publication is
temporarily unavailable. Idle repairs the projection when possible and clears
it when no plan exists or every item is complete. The workflow never infers a
goal, completion, or blockage from conversation text.

Each accepted update also publishes Euler's bounded `plan-presentation`
checklist for the transcript. Presentation is best-effort like the context
projection; the private JSON state remains the workflow authority. Terminal
idle republishes the current durable revision before deciding to continue or
stop, so a transient presentation failure can heal on the next boundary. If
an append completed but its durability result was ambiguous, Euler's writer
fences that session as designed; after the session is reopened, idle can
reconcile the already-durable presentation without duplicating it.

The entrypoint imports the canonical Python managed-process SDK from
`sdks/python/euler-managed-process-sdk`; this package does not vendor its own
SDK copy.

## Enable it

From a pinned checkout of this repository:

```sh
euler extension validate extensions/plan-todo
euler extension link extensions/plan-todo
euler extension enable plan-todo
```

Then start Euler, or start a new session if Euler was already running. The
extension contributes `update_plan` directly to the model; there is no
user-invoked extension command or slash command to run. It occupies Euler's
single terminal-idle contributor slot, so no other enabled extension may claim
that slot in the same session.

Version 0.1 has no automatic state migration or reset command. If a state file
is reported corrupt, exit Euler, inspect and then remove
`extensions/plan-todo/plan.json` beneath that session's provenance directory,
and resume the session. Euler will start a new plan only after the model next
calls `update_plan`.

## Tests

From the repository root:

```sh
python -m unittest discover -s extensions/plan-todo/python/tests -v
```
