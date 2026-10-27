# CodeSwarm Review

`code-swarm` runs 1 to 5 review-only agents over material the calling agent
supplies and writes one consolidated review artifact. It needs Euler with
extension config documents, model-target arguments, and model-tool input
budgets (ADR 0022).

## Review: the `code_swarm_review` model tool

`review` is agent-only. Its model tool takes:

- `focus` (required): the review question, at most 7 KiB.
- `context` (required): the exact material to review, at most 256 KiB. The
  agent gathers it with ordinary tools first.
- `personas` (optional): 1 to 5 of `correctness`, `safety`, `tests`. Defaults
  rotate through all three.
- `models` (optional): up to 5 `provider::model` targets, only when the user
  named them. An empty list means no override.
- `max_tokens` (optional): per-reviewer output budget.

The tool declares `max_input_bytes` of 320 KiB, the host cap, so a full
context of ordinary text fits. The host bounds the serialized input, so
escape-heavy material (quotes, control characters) can exceed the cap before
the context bound; the host then rejects the call with its own size error.

Reviewers resolve in this order: explicit `models`, then the project config,
then the user config. With none, the review does not run. A malformed config
is an error, never a fall-through to the next tier.

A review that ran returns `"review_ran": true` with the K-of-N summary, the
artifact reference, and each reviewer's ok/error and findings (bounded to
16 KiB per reviewer in the result; the artifact keeps the full text). A review
that did not run returns `"review_ran": false` with a `reason`
(`unconfigured`, `invalid_config`, `invalid_input`, `input_too_large`,
`reviewer_batch_failed`) and a
`message` the agent relays to the user. These are successful results because
managed-process command errors reach the model only as a generic failure.

## Configuration

Config is data, not authority: spawning reviewers still needs `agent-spawn`.
The host stores the documents at `<workspace>/.euler/extensions/code-swarm/config.json`
(project) and `<euler home>/extensions/code-swarm/config.json` (user), in the
legacy `.euler/code-swarm.json` shape:

```json
{"version": 1, "reviewers": [{"target": "provider::model", "persona": "tests"}], "max_tokens": 8192}
```

Personas label every reviewer or none.

| Command | Writes | Input |
|---|---|---|
| `configure` | project | `--models a::b,c::d` (1 to 5); the TUI opens the model picker |
| `configure-user` | user | same |
| `import` | project | a legacy config document as JSON input |
| `import-user` | user | same |

```sh
euler extension run code-swarm.configure <session> --models anthropic::claude-opus-5,openai::gpt-5.5
euler extension run code-swarm.import <session> --input-file .euler/code-swarm.json
euler extension run code-swarm.import-user <session> --input-file ~/.euler/code-swarm.json
```

`configure` replaces the tier's document with targets only, so personas and
`max_tokens` from an earlier import are dropped. A config command returns `"written": true`
with the tier and targets, or `"written": false` with a `reason` and
`message` when it refuses the input.
