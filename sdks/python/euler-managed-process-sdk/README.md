# Euler managed-process SDK for Python

This is the canonical Python client for Euler's language-neutral
`euler-managed-process/1` protocol. It is dependency-free at runtime and
requires Python 3.9 or newer.

For local development, either install the package:

```sh
python -m pip install -e sdks/python/euler-managed-process-sdk
```

or add its `src` directory to the entrypoint's import path. Extensions in this
repository use the latter form so a pinned extension-source checkout remains
self-contained without keeping a private SDK copy in every extension.

An extension supplies command handlers and starts the one-command process:

```python
from euler_managed_process_sdk import serve

serve({"my-command": handle_my_command})
```

Handlers receive a `CommandContext` and return a JSON object. Capability-gated
host operations are available through `context.host`; capability declarations
and protocol limits remain host-owned. In particular, `Host.state_dir()` is
the session- and extension-namespaced private state surface governed by
`extension-state`; it does not grant workspace filesystem authority.
`Host.update_plan_presentation(...)` publishes a bounded structured checklist
to Euler's canonical transcript/provenance event stream; it does not inject
workflow text into model context.

`Host.query_provenance(..., through_event_id=event_id)` applies an inclusive,
filter-independent durable-prefix upper bound. The SDK omits that additive
wire field when it is `None`, preserving ordinary-query compatibility with
older `euler-managed-process/1` hosts. Request-tick extensions should pass the
exact host-supplied cutoff on every page.

`extension_model_text_is_format_safe(text)` exposes Euler's frozen Unicode-17
format-spoof and separator predicate for extension-authored model-facing text.
It intentionally leaves ordinary control-character, emptiness, and length
rules to the specific host operation or workflow.

Protocol conformance tests exercise the production package directly, including
every public host method, cancellation, framing, and sanitized failures:

```sh
python -m unittest discover \
  -s sdks/python/euler-managed-process-sdk/tests -v
```
