# Python session summary

This managed-process extension reads a bounded provenance page, counts event
kinds, and writes `python-session-summary.json` as an Euler artifact.

From a pinned checkout of this extension source:

```sh
euler extension link extensions/python-session-summary
euler extension enable python-session-summary
euler extension run python-session-summary.summarize SESSION_ID
```

The entrypoint imports the shared SDK from `sdks/python`; it carries no private
SDK copy and needs no virtual environment.
