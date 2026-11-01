"""The schema documentation must not drift from the schema."""

from __future__ import annotations

import subprocess
import sys


def test_logging_format_field_reference_matches_the_models(repository_root):
    result = subprocess.run(
        [sys.executable, "scripts/generate_logging_docs.py", "--check"],
        cwd=str(repository_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "docs/LOGGING_FORMAT.md is out of date with archipelago_logging.events.\n"
        "Regenerate it with: python scripts/generate_logging_docs.py --fields-only\n"
        + result.stdout
        + result.stderr
    )


def test_every_event_type_is_documented(repository_root):
    from archipelago_logging.events import EVENT_MODELS

    text = (repository_root / "docs" / "LOGGING_FORMAT.md").read_text(encoding="utf-8")
    for type_name in EVENT_MODELS:
        assert f"### `{type_name}`" in text, f"{type_name} has no field table"
        assert f"#### `{type_name}`" in text, f"{type_name} has no worked example"


def test_every_documented_example_is_a_valid_record(repository_root):
    """The worked examples must parse against the models they illustrate."""
    import json
    import re

    from archipelago_logging import parse_event

    text = (repository_root / "docs" / "LOGGING_FORMAT.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert blocks, "no worked examples found"

    checked = 0
    for block in blocks:
        record = json.loads(block)
        if "type" not in record or "run_id" not in record:
            continue
        for key, value in list(record.items()):
            if isinstance(value, dict) and "__elided__" in value:
                record[key] = {}
        parsed = parse_event(record)
        assert parsed.type == record["type"]
        checked += 1
    assert checked >= 8, f"only {checked} example records were validated"


def test_the_api_reference_matches_the_service(repository_root):
    result = subprocess.run(
        [sys.executable, "scripts/generate_api_docs.py", "--check"],
        cwd=str(repository_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "docs/API.md is out of date with the service.\n"
        "Regenerate it with: python scripts/generate_api_docs.py\n" + result.stdout + result.stderr
    )
