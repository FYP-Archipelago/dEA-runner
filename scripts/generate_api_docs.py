#!/usr/bin/env python3
"""Regenerate the reference section of ``docs/API.md`` from the Runner service itself.

The endpoints, statuses and schemas come from the app's OpenAPI document (what ``/docs`` shows),
and the override bounds and presets from ``service/presets.py``, so the document cannot drift from
the service; ``tests/test_docs_are_current.py`` runs ``--check``. Nothing here needs Docker or
Redis.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

DOCUMENT = REPOSITORY_ROOT / "docs" / "API.md"
BEGIN = "<!-- BEGIN GENERATED API REFERENCE -->"
END = "<!-- END GENERATED API REFERENCE -->"


def _type(schema: dict[str, Any]) -> str:
    """A JSON schema as something a reader of JSON recognises."""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[1]
    if "enum" in schema:
        return " \\| ".join(json.dumps(value) for value in schema["enum"])
    if "anyOf" in schema:
        return " \\| ".join(_type(option) for option in schema["anyOf"])
    kind = schema.get("type")
    if kind == "array":
        return f"{_type(schema.get('items', {}))}[]"
    if kind == "object":
        values = schema.get("additionalProperties")
        return f"object of {_type(values)}" if isinstance(values, dict) and values else "object"
    if kind == "integer":
        return "int"
    return kind or "any"


def _value(value: Any) -> str:
    """A default as JSON shows it: true, not True; 20,000 for a number."""
    if isinstance(value, bool):
        return json.dumps(value)
    return f"{value:,}" if isinstance(value, int) else str(value)


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().replace("|", "\\|") or "—"


def _fields(schema: dict[str, Any]) -> str:
    required = set(schema.get("required", []))
    lines = ["| Field | Type | Required | Meaning |", "| --- | --- | --- | --- |"]
    for name, field in schema.get("properties", {}).items():
        lines.append(
            f"| `{name}` | `{_type(field)}` | {'yes' if name in required else 'no'} "
            f"| {_cell(field.get('description', ''))} |"
        )
    return "\n".join(lines)


def _operation(method: str, path: str, operation: dict[str, Any]) -> str:
    parts = [f"### `{method.upper()} {path}`", "", f"**{operation['summary']}.** {_cell(operation.get('description', ''))}"]
    for parameter in operation.get("parameters", []):
        parts += ["", f"Path parameter `{parameter['name']}`: {_cell(parameter.get('description', ''))}"]
    body = operation.get("requestBody")
    if body:
        schema = body["content"]["application/json"]["schema"]
        parts += ["", f"Body: `{_type(schema)}` (below)."]
    parts += ["", "| Status | Body | When |", "| --- | --- | --- |"]
    for status, response in operation["responses"].items():
        schema = response.get("content", {}).get("application/json", {}).get("schema", {})
        parts.append(f"| {status} | `{_type(schema)}` | {_cell(response['description'])} |")
    return "\n".join(parts)


def build_reference() -> str:
    """Every endpoint, every schema, the override bounds and the presets."""
    from orchestrator.settings import ServiceSettings
    from service import create_app
    from service.presets import OVERRIDES, load_presets

    spec = create_app(ServiceSettings()).openapi()
    parts = [f"Generated from the service (version {spec['info']['version']}) by "
             "`scripts/generate_api_docs.py`; the Swagger UI at `/docs` shows the same document.", ""]
    parts += ["### Endpoints", ""]
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            parts += [_operation(method, path, operation), ""]
    parts += ["### Schemas", ""]
    for name, schema in spec["components"]["schemas"].items():
        parts += [f"#### `{name}`", "", _cell(schema.get("description", "")), "", _fields(schema), ""]
    parts += ["### Override bounds", "", "| Key | Type | Bounds |", "| --- | --- | --- |"]
    for key, rule in OVERRIDES.items():
        bounds = [f"{label} {rule[name]:,}" for name, label in (("min", "≥"), ("max", "≤")) if name in rule]
        if "choices" in rule:
            bounds.append(" · ".join(f"`{choice}`" for choice in rule["choices"]))
        if rule["type"] == "bool":
            bounds.append("`true` · `false`")
        if rule.get("optional"):
            bounds.append("optional, no default")
        parts.append(f"| `{key}` | {rule['type']} | {', '.join(bounds) or '—'} |")
    keys = [key for key, rule in OVERRIDES.items() if not rule.get("optional")]
    parts += ["", "### Presets and their defaults", "",
              "| Preset | Algorithm | Benchmark | " + " | ".join(f"`{key}`" for key in keys) + " |",
              "| --- | --- | --- | " + " | ".join("---" for _ in keys) + " |"]
    for preset in load_presets().values():
        values = " | ".join(_value(preset.defaults[key]) for key in keys)
        parts.append(f"| `{preset.name}` | {preset.algorithm} | {preset.benchmark} | {values} |")
    return "\n".join(parts)


def _replace(text: str, reference: str) -> str:
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(f"markers {BEGIN} / {END} not found in {DOCUMENT}")
    return pattern.sub(lambda _match: f"{BEGIN}\n{reference}\n{END}", text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit non-zero if the document is stale.")
    args = parser.parse_args()
    current = DOCUMENT.read_text(encoding="utf-8")
    updated = _replace(current, build_reference())
    if args.check:
        if updated != current:
            print(f"{DOCUMENT} is out of date with the service.\n"
                  "Regenerate it with: python scripts/generate_api_docs.py", file=sys.stderr)
            return 1
        print(f"{DOCUMENT} is current.")
        return 0
    DOCUMENT.write_text(updated, encoding="utf-8")
    print(f"Regenerated {DOCUMENT}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
