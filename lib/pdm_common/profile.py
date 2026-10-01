"""Asset profile loading (CFG-01) and its content hash (CFG-07).

The asset profile is the single source of asset-specific facts: signals,
topics, sampling and detector inputs. Services call ``load_profile`` at
start-up and stamp what they publish with ``profile_hash``.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from pdm_common.hashing import sha256_file

# Keys every service depends on. Version Zero checks only these; the full
# schema (CFG-02) replaces this list with a pydantic model.
REQUIRED_KEYS = (
    ("asset", "id"),
    ("topics", "telemetry"),
    ("topics", "payload_schema"),
    ("signals", "analog"),
    ("signals", "digital"),
)


class ProfileError(ValueError):
    """The asset profile cannot be read or does not validate."""


def read_yaml(path: Path, error=ProfileError, what: str = "asset profile"):
    """Parse a YAML file; syntax errors name the file and line."""
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise error(f"{path}: cannot read {what}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else "?"
        problem = getattr(exc, "problem", None) or exc
        raise error(f"{path}:{line}: not valid YAML: {problem}") from exc
    if not isinstance(data, dict):
        raise error(f"{path}: {what} must be a YAML mapping")
    return data


def load_profile(path: str | Path) -> dict:
    """Load an asset profile and check the keys every service depends on."""
    profile = read_yaml(Path(path))
    for keys in REQUIRED_KEYS:
        node = profile
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                raise ProfileError(
                    f"{path}: asset profile is missing required key: {'.'.join(keys)}"
                )
            node = node[key]
    return profile


def profile_hash(path: str | Path) -> str:
    """SHA-256 of the profile file: the version stamp on every output (CFG-07)."""
    return sha256_file(path)


def signal_names(profile: dict) -> list[str]:
    """All signal names, analog then digital, in profile order."""
    return [s["name"] for s in profile["signals"]["analog"]] + [
        s["name"] for s in profile["signals"]["digital"]
    ]


def events_file(profile: dict, profile_path: str | Path) -> Path:
    """The events file named in the profile, resolved next to the profile."""
    name = (profile.get("references") or {}).get("events_file", "events.yaml")
    return Path(profile_path).resolve().parent / name
