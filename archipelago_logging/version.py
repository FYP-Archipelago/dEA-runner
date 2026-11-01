"""Version constants for the Archipelago log format and the Runner."""

from __future__ import annotations

SCHEMA_MARKER = "2"
"""The value of every record's ``schema_version``. Not a version: the format is not versioned, and
this never changes. The field exists only because the Archipelago Backend's clustering module
requires ``run_start.schema_version`` with major version 2; it goes once the backend drops that
check (on the list of things to tell them)."""

HARNESS_VERSION = "0.5.0"
"""The Runner's own version, recorded in ``run_start.harness_version``."""
