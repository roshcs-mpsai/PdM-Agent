"""Procedural skills for the diagnostic agent (T32; PR-11, AGT-06).

A skill is a folder of Markdown files: ``SKILL.md``, which starts with YAML
front matter (``name``, ``description``, ``version`` as a date or semver and,
for an asset skill, ``asset``), and optionally more ``.md`` files that the
agent reads after it. There are two kinds:

* shared skills, one folder each under ``skills/shared/``: how to triage an
  incident and how to write a brief, the same for every asset;
* the asset skill, the folder the asset profile names in
  ``references.skills_folder``: the asset's failure modes, what its checks
  mean, the look-alikes, and the inspection tasks and safety notes.

``load_skills`` reads the shared skills (by folder name) and then the asset's
folder. ``versions`` gives each skill as ``version@sha12`` for the agent trace
and the brief's ``versions.skills``, and ``system_text`` joins the skills for
the system prompt, each inside a ``<skill ...>`` block whose header names it.

The sha256 covers every Markdown file of the folder (SKILL.md first, the rest
by file name; each as its name and its text with LF line endings), so any
edit, rename or added file changes the recorded version even when the
front-matter version is not bumped, and a checkout that converts line endings
does not. A new asset adds its own folder and points its profile at it; the
shared skills stay as they are. Nothing here names an asset signal (CFG-03).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_DIR = Path("skills") / "shared"
SKILL_FILE = "SKILL.md"
KINDS = ("shared", "asset")
REQUIRED = ("name", "description", "version")
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)", re.DOTALL | re.MULTILINE)
WORD = re.compile(r"[A-Za-z0-9]")
CLOSE = "</skill>"


class SkillError(RuntimeError):
    """A skill folder is missing, incomplete or malformed."""


@dataclass(frozen=True)
class Skill:
    name: str
    kind: str                       # "shared" or "asset"
    path: Path                      # the skill's folder
    text: str                       # what the model reads: the folder's Markdown without front matter
    sha256: str                     # of the folder's Markdown files (names and LF-normalised text)
    version: str                    # SKILL.md's front-matter version
    description: str = ""
    files: tuple[str, ...] = ()     # the Markdown files, in the order read
    asset: str | None = None        # the asset id, for an asset skill

    @property
    def stamp(self) -> str:
        """``version@sha12``: what traces and briefs record."""
        return f"{self.version}@{self.sha256[:12]}"

    @property
    def words(self) -> int:
        """Words in ``text``: whitespace-separated tokens holding a letter or digit (table rules do not count)."""
        return sum(1 for w in self.text.split() if WORD.search(w))


def _read(path: Path) -> str:
    """A Markdown file as text with LF line endings and no byte-order mark."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"{path}: cannot read skill file: {exc}") from exc
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def split_front_matter(text: str, path: Path | str = "<text>") -> tuple[dict | None, str]:
    """(front matter as a dict, or None if the text has none; the body after it)."""
    m = FRONT_MATTER.match(text)
    if not m:
        return None, text
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise SkillError(f"{path}: front matter is not valid YAML: {exc}") from exc
    except (ValueError, TypeError, OverflowError) as exc:   # a well-formed value that cannot be built: 2026-13-45
        raise SkillError(f"{path}: front matter holds a value that cannot be read: {exc}") from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise SkillError(f"{path}: front matter must be a YAML mapping")
    return meta, text[m.end():]


def _version(value, path: Path) -> str:
    if isinstance(value, dt.date):
        value = value.isoformat()
    if not isinstance(value, str):
        raise SkillError(f"{path}: version {value!r} must be a date (YYYY-MM-DD) or semver (1.2.3); quote it")
    if DATE.match(value):
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            raise SkillError(f"{path}: version {value!r} is not a real date") from None
    elif not SEMVER.match(value):
        raise SkillError(f"{path}: version {value!r} must be a date (YYYY-MM-DD) or semver (1.2.3)")
    return value


def load_skill(folder: Path | str, kind: str, asset_id: str | None = None) -> Skill:
    """One skill folder: SKILL.md first, then every other ``.md`` file in it by name."""
    folder = Path(folder)
    if kind not in KINDS:
        raise SkillError(f"{folder}: kind must be one of {KINDS}, not {kind!r}")
    skill_md = folder / SKILL_FILE
    if not folder.is_dir():
        raise SkillError(f"{folder}: no such skill folder")
    if not skill_md.is_file():
        raise SkillError(f"{folder}: no {SKILL_FILE} (every skill folder needs one, with front matter)")
    others = sorted(p for p in folder.glob("*.md") if p.is_file() and p.name.lower() != SKILL_FILE.lower())
    digest = hashlib.sha256()
    bodies, meta = [], None
    for path in [skill_md, *others]:
        text = _read(path)
        digest.update(path.name.encode() + b"\0" + text.encode() + b"\0")
        fm, body = split_front_matter(text, path)
        if path is skill_md:
            meta = fm
        if body.strip():
            bodies.append(body.strip())
    if meta is None:
        raise SkillError(f"{skill_md}: no front matter (a '---' block with {', '.join(REQUIRED)} at the top)")
    missing = [k for k in REQUIRED if not str(meta.get(k) or "").strip()]
    if missing:
        raise SkillError(f"{skill_md}: front matter lacks {', '.join(missing)}")
    name = str(meta["name"]).strip()
    if name != folder.name:
        raise SkillError(f"{skill_md}: name {name!r} does not match its folder {folder.name!r}")
    asset = meta.get("asset")
    if kind == "asset" and asset != asset_id:
        raise SkillError(f"{skill_md}: an asset skill names its asset in front matter (asset: {asset_id}), "
                         f"not {asset!r}")
    if kind == "shared" and asset is not None:
        raise SkillError(f"{skill_md}: a shared skill names no asset (found asset: {asset})")
    text = "\n\n".join(bodies)
    if not text:
        raise SkillError(f"{folder}: the skill has no text")
    return Skill(name=name, kind=kind, path=folder, text=text, sha256=digest.hexdigest(),
                 version=_version(meta["version"], skill_md), description=str(meta["description"]).strip(),
                 files=tuple(p.name for p in [skill_md, *others]), asset=asset)


def load_skills(profile: dict, root: Path | str | None = None) -> list[Skill]:
    """The shared skills (skills/shared/*/, by folder name), then the asset skill the profile names
    in ``references.skills_folder``; folders resolve against ``root`` (default: this repository)."""
    root = Path(root) if root is not None else REPO_ROOT
    shared_dir = root / SHARED_DIR
    if not shared_dir.is_dir():
        raise SkillError(f"{shared_dir}: no shared skills folder")
    folders = sorted(d for d in shared_dir.iterdir() if d.is_dir() and not d.name.startswith((".", "_")))
    if not folders:
        raise SkillError(f"{shared_dir}: no shared skills (one folder per skill, each with {SKILL_FILE})")
    skills = [load_skill(d, "shared") for d in folders]
    rel = (profile.get("references") or {}).get("skills_folder")
    if not rel:
        raise SkillError("the asset profile names no skill folder (references.skills_folder)")
    folder = root / rel
    if not folder.is_dir():
        raise SkillError(f"{folder}: the asset skill folder named in the profile "
                         f"(references.skills_folder: {rel}) does not exist")
    skills.append(load_skill(folder, "asset", asset_id=(profile.get("asset") or {}).get("id")))
    versions(skills)                                   # names must be unique
    return skills


def versions(skills: list[Skill]) -> dict[str, str]:
    """{skill name: ``version@sha12``} for the agent trace and the brief's ``versions.skills``."""
    out: dict[str, str] = {}
    for s in skills:
        if s.name in out:
            raise SkillError(f"two skills are named {s.name!r}")
        out[s.name] = s.stamp
    return out


def system_text(skills: list[Skill]) -> str:
    """The skills for the system prompt, in the order given, each as
    ``<skill name=".." kind=".." version="version@sha12">`` + its text + ``</skill>``."""
    blocks = []
    for s in skills:
        if CLOSE in s.text:
            raise SkillError(f"{s.path}: the skill's text contains {CLOSE}, which would end its block early")
        blocks.append(f'<skill name="{s.name}" kind="{s.kind}" version="{s.stamp}">\n{s.text}\n{CLOSE}')
    return "\n\n".join(blocks) + "\n"


def main(argv=None) -> int:
    import argparse

    from pdm_common.profile import load_profile

    ap = argparse.ArgumentParser(prog="python -m pdm_common.skills",
                                 description="The skills the agent loads for an asset: versions and sizes, "
                                             "or the system-prompt text.")
    ap.add_argument("--profile", required=True, help="the asset profile (asset_profile.yaml)")
    ap.add_argument("--root", default=str(REPO_ROOT), help="the repository root (default: this checkout)")
    ap.add_argument("--text", action="store_true", help="print the system-prompt text instead of the table")
    args = ap.parse_args(argv)
    skills = load_skills(load_profile(args.profile), args.root)
    if args.text:
        print(system_text(skills), end="")
        return 0
    for s in skills:
        print(f"{s.name:<16} {s.kind:<6} {s.stamp:<24} {s.words:>5} words  {', '.join(s.files)}")
    print(f"{'total':<16} {'':<6} {'':<24} {sum(s.words for s in skills):>5} words")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
