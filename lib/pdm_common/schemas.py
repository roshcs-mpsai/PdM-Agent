"""The payload schemas (T17; FRD section 9): schemas/<name>.json, JSON Schema 2020-12.

Every MQTT payload, tool result and brief has a schema in schemas/;
``validate(name, doc)`` returns the problems (empty when the document is
valid) and ``check(name, doc)`` raises. tests/test_schemas.py runs each
service's real output through its schema, so a payload and its documentation
cannot drift apart.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


class SchemaError(ValueError):
    pass


def names() -> list[str]:
    return sorted(p.stem for p in SCHEMA_DIR.glob("*.json"))


@lru_cache(maxsize=None)
def validator(name: str) -> Draft202012Validator:
    path = SCHEMA_DIR / f"{name}.json"
    if not path.exists():
        raise SchemaError(f"no schema {name} in {SCHEMA_DIR}")
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(name: str, doc) -> list[str]:
    """Every problem as 'path: message', path in JSON-pointer style; [] when valid."""
    if isinstance(doc, (str, bytes)):
        doc = json.loads(doc)
    errors = sorted(validator(name).iter_errors(doc), key=lambda e: list(e.absolute_path))
    return [f"/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors]


def check(name: str, doc) -> None:
    problems = validate(name, doc)
    if problems:
        raise SchemaError(f"{name}: " + "; ".join(problems))
