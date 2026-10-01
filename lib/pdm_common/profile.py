"""Asset profile loading (CFG-01, CFG-02) and its content hash (CFG-07).

The asset profile is the single source of asset-specific facts: signals,
topics, sampling and detector inputs. Services call ``load_profile`` at
start-up, which validates the file against the versioned schema in
``pdm_common.schema``, and stamp what they publish with ``profile_hash``.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from pdm_common.hashing import sha256_file


class ProfileError(ValueError):
    """The asset profile cannot be read or does not validate."""


def read_yaml_text(path: Path, error=ProfileError, what: str = "asset profile"):
    """Read and parse a YAML file -> (text, data); errors name the file and line."""
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
    except (ValueError, TypeError, OverflowError) as exc:
        # a well-formed value that cannot be built, such as the date 2020-04-31
        raise error(f"{path}:{_unbuildable_line(text)}: cannot read value: {exc}") from exc
    if not isinstance(data, dict):
        raise error(f"{path}: {what} must be a YAML mapping")
    return text, data


def _unbuildable_line(text: str):
    """Line of the first scalar PyYAML parses but cannot construct."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return "?"
    builder = yaml.SafeLoader("")
    stack = [root]
    bad = []
    while stack:
        node = stack.pop()
        if isinstance(node, yaml.MappingNode):
            stack += [n for pair in node.value for n in pair]
        elif isinstance(node, yaml.SequenceNode):
            stack += node.value
        elif node is not None:
            try:
                builder.construct_object(node)
            except Exception:
                bad.append(node.start_mark.line + 1)
    return min(bad) if bad else "?"


def read_yaml(path: Path, error=ProfileError, what: str = "asset profile"):
    """Parse a YAML file; syntax errors name the file and line."""
    return read_yaml_text(path, error, what)[1]


def load_profile(path: str | Path) -> dict:
    """Load an asset profile and validate it against the schema (CFG-02).

    Returns the profile as a plain dict, exactly as written. A profile that
    does not validate raises ProfileError listing every problem as
    ``file:line: key.path: message``.
    """
    from pdm_common.schema import validate_profile   # pydantic only when needed

    text, profile = read_yaml_text(Path(path))
    validate_profile(profile, text, path, error=ProfileError)
    return profile


def profile_hash(path: str | Path) -> str:
    """SHA-256 of the profile file: the version stamp on every output (CFG-07)."""
    return sha256_file(path)


def signal_names(profile: dict) -> list[str]:
    """All signal names, analog then digital, in profile order."""
    return [s["name"] for s in profile["signals"]["analog"]] + [
        s["name"] for s in profile["signals"]["digital"]
    ]


def analog_names(profile: dict) -> list[str]:
    return [s["name"] for s in profile["signals"]["analog"]]


def digital_names(profile: dict) -> list[str]:
    return [s["name"] for s in profile["signals"]["digital"]]


def events_file(profile: dict, profile_path: str | Path) -> Path:
    """The events file named in the profile, resolved next to the profile."""
    name = (profile.get("references") or {}).get("events_file", "events.yaml")
    return Path(profile_path).resolve().parent / name
