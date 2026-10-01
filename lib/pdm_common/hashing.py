"""Content hashes and canonical JSON (CFG-07, NFR-05)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """SHA-256 of a file's bytes, hex. Matches ``shasum -a 256 <file>``."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(obj) -> str:
    """Sorted keys, no whitespace: identical content gives identical bytes.

    Every MQTT payload uses this form (FRD section 9), so message logs from
    two runs of the same replay can be compared byte for byte.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))
