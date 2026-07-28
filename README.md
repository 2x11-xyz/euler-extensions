# Euler Extensions

Extensions for [Euler](https://github.com/2x11-xyz/euler): a research agent
(coding agent included) and an open-ended, runtime-extensible platform.

This repository is an Euler extension source. Euler installs it as a pinned
git source into the `~/.euler` extension store, per ADR 0015 in the Euler
repository. Extensions here run as separate processes over the
managed-process protocol and can be written in any language; per-language
SDKs live here as conveniences, never requirements.

## Status

The Rust SDK (`sdks/rust/euler-managed-process-sdk`) and converted Rust
extensions are in and CI-covered: `session-export`, `diagnostics-report`,
`code-swarm`, `maxproof`, and `autoresearch`; each retains end-to-end result
parity with its formerly bundled counterpart. The canonical Python SDK
(`sdks/python/euler-managed-process-sdk`) is exercised by `python-proof`,
`python-session-summary`, and `plan-todo`. Plan/todo is the first Python
consumer of model-tool registration and terminal-idle contribution; its
workflow policy remains entirely outside Euler core.

`causal-dag` is present as a spec package (`extensions/causal-dag`), preserving
the schemas and golden fixtures its Python implementation must satisfy. Layout:

```text
extensions/<id>/    one extension per directory (Euler.extension.json + entrypoint)
sdks/               per-language authoring SDKs
themes/             theme files
templates/          prompt and brief templates
```
