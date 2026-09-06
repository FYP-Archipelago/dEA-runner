"""Version constants for the Archipelago standardized log format."""

from __future__ import annotations

SCHEMA_VERSION = "2.0"

HARNESS_VERSION = "0.3.0"

SCHEMA_VERSION_INFO = tuple(int(part) for part in SCHEMA_VERSION.split("."))


def is_compatible(version: str) -> bool:
    """Return ``True`` if a log written at ``version`` is readable by this code."""
    try:
        major, _minor = (int(part) for part in version.split(".")[:2])
    except (TypeError, ValueError):
        return False
    return major == SCHEMA_VERSION_INFO[0]
