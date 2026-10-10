"""The data catalogue (T3; ING-13): one JSON line per processed dataset.

Each entry says what a dataset is (a window table written by
tools/build_windows.py, or the windows a replay produced through ingest),
how big it is (rows and columns), which manifest describes it (its SHA-256)
and where it came from (lineage: the raw file, profile, events and feature
declarations by hash, and the code that made it). The harness looks a
processed folder up here before it evaluates it.

The catalogue lives beside the data it describes, ``data/catalogue.jsonl``
by default (``$PDM_CATALOGUE`` overrides it), and like the data it stays out
of git.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from pdm_common.hashing import canonical_json

ENTRY_SCHEMA = "catalogue_entry_v1"
KINDS = ("window_table", "stream_windows", "grid", "scores")


def default_path() -> Path:
    return Path(os.environ.get("PDM_CATALOGUE", "data/catalogue.jsonl"))


def _sha(entry: dict) -> str:
    import hashlib
    core = {k: v for k, v in entry.items() if k not in ("recorded_at", "entry_sha256")}
    return hashlib.sha256(canonical_json(core).encode()).hexdigest()


def add(dataset: str, kind: str, *, rows: int, columns: int, manifest_sha256: str | None,
        lineage: dict, path=None, note: str | None = None) -> dict:
    """Append one entry and return it."""
    if kind not in KINDS:
        raise ValueError(f"unknown dataset kind {kind!r}: use one of {', '.join(KINDS)}")
    entry = {"schema": ENTRY_SCHEMA, "dataset": dataset, "kind": kind, "rows": int(rows), "columns": int(columns),
             "manifest_sha256": manifest_sha256, "lineage": lineage}
    if note:
        entry["note"] = note
    entry["entry_sha256"] = _sha(entry)
    entry["recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    path = Path(path) if path else default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def entries(path=None) -> list[dict]:
    path = Path(path) if path else default_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def find(path=None, **match) -> list[dict]:
    """Entries whose fields equal every ``match`` value, oldest first."""
    return [e for e in entries(path) if all(e.get(k) == v for k, v in match.items())]
