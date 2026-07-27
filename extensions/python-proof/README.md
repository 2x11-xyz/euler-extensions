# Python proof

A minimal managed-process extension that exercises the canonical Python SDK.
It reads one bounded provenance page and writes a small JSON artifact.

From a pinned checkout of this extension source:

```sh
euler extension link extensions/python-proof
euler extension enable python-proof
euler extension run python-proof.inspect SESSION_ID
```

The entrypoint imports the shared SDK from `sdks/python`; it carries no private
SDK copy and needs no virtual environment.
