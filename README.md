# Euler Extensions

Extensions for [Euler](https://github.com/2x11-xyz/euler): a research agent
(coding agent included) and an open-ended, runtime-extensible platform.

This repository is an Euler extension source. Euler installs it as a pinned
git source into the `~/.euler` extension store, per ADR 0015 in the Euler
repository. Extensions here run as separate processes over the
managed-process protocol and can be written in any language; per-language
SDKs live here as conveniences, never requirements.

## Status

Growing. The Rust SDK (`sdks/rust/euler-managed-process-sdk`) and the first
converted extension (`extensions/session-export`) are in; the remaining
bundled extensions migrate as the host boundary proves out. Layout:

```text
extensions/<id>/    one extension per directory (Euler.extension.json + entrypoint)
sdks/               per-language authoring SDKs
themes/             theme files
templates/          prompt and brief templates
```
