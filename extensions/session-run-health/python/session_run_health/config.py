"""Closed, session-local configuration for run-health recurrence policy."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Dict


CONFIG_FILENAME = "run-health-config.json"
CONFIG_SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 16 * 1024

DEFAULT_THRESHOLDS: Dict[str, int] = {
    "failure_recurrence": 3,
    "edit_recurrence": 5,
    "no_progress_seconds": 300,
    "context_window": 4,
    "minimum_context_tokens": 8_000,
    "context_growth_min_tokens": 8_000,
    "context_growth_percent": 50,
    "maximum_cache_reuse_percent": 10,
    "active_run_input_recurrence": 1,
}

_BOUNDS = {
    "failure_recurrence": (2, 20),
    "edit_recurrence": (2, 50),
    "no_progress_seconds": (30, 86_400),
    "context_window": (3, 8),
    "minimum_context_tokens": (1_024, 2_000_000),
    "context_growth_min_tokens": (1_024, 1_000_000),
    "context_growth_percent": (10, 500),
    "maximum_cache_reuse_percent": (0, 90),
    "active_run_input_recurrence": (1, 10),
}


class ConfigurationError(ValueError):
    """The explicit run-health configuration violates its closed schema."""


def load_thresholds(state_directory: str) -> Dict[str, int]:
    """Load optional session-local overrides, or return documented defaults."""

    path = Path(state_directory) / CONFIG_FILENAME
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return dict(DEFAULT_THRESHOLDS)
    except OSError as error:
        raise ConfigurationError("run-health configuration could not be inspected") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigurationError("run-health configuration must be a regular file")
    if metadata.st_size > MAX_CONFIG_BYTES:
        raise ConfigurationError("run-health configuration exceeds its size limit")
    try:
        encoded = path.read_bytes()
    except OSError as error:
        raise ConfigurationError("run-health configuration could not be read") from error
    if len(encoded) > MAX_CONFIG_BYTES:
        raise ConfigurationError("run-health configuration exceeds its size limit")
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ConfigurationError("run-health configuration is not valid JSON") from error
    return parse_thresholds(value)


def parse_thresholds(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        raise ConfigurationError("run-health configuration must be an object")
    allowed = {"schema_version", *_BOUNDS}
    if set(value) - allowed:
        raise ConfigurationError("run-health configuration has unknown fields")
    if value.get("schema_version") != CONFIG_SCHEMA_VERSION or isinstance(
        value.get("schema_version"), bool
    ):
        raise ConfigurationError("run-health configuration schema_version must be 1")

    thresholds = dict(DEFAULT_THRESHOLDS)
    for name, bounds in _BOUNDS.items():
        if name not in value:
            continue
        candidate = value[name]
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            raise ConfigurationError(f"run-health {name} must be an integer")
        minimum, maximum = bounds
        if not minimum <= candidate <= maximum:
            raise ConfigurationError(
                f"run-health {name} must be between {minimum} and {maximum}"
            )
        thresholds[name] = candidate
    return thresholds
