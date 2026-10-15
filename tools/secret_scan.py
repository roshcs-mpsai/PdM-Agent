r"""Secret scan with the rules in .gitleaks.toml, without the gitleaks binary (R3, NFR-06).

    python tools/secret_scan.py              # every file git tracks (git ls-files)
    python tools/secret_scan.py --staged     # the staged version of every staged file: the pre-commit hook
    python tools/secret_scan.py FILE...      # these files, tracked or not

It applies each [[rules]] entry of .gitleaks.toml (regex, secretGroup, path, keywords; a rule
with a path and no regex flags the file itself) and the [allowlist] (paths, regexes and
stopwords, tested against the secret), skips binary files, and ignores a finding whose line
carries a "gitleaks:allow" comment, as gitleaks does. Unlike gitleaks it has no built-in
rules and no entropy checks, and it reads files, not history.

Output names the file, line and rule, never the secret. Exit status: 0 nothing found,
1 findings, 2 the scan could not run (no git repository, a bad config).

Pre-commit hook, once per clone:

    printf '#!/bin/sh\nexec python3 tools/secret_scan.py --staged\n' > .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit

Python 3.10 has no tomllib; there the config is read by a small parser for the part of TOML
the file uses (tests/test_secrets.py checks it gives what tomllib gives).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

CONFIG_NAME = ".gitleaks.toml"
ALLOW_MARK = "gitleaks:allow"
BINARY_PROBE = 8000


class ScanError(Exception):
    """The scan cannot run: exit status 2."""


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    regex: Optional[re.Pattern]
    secret_group: int
    path: Optional[re.Pattern]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Allowlist:
    paths: tuple[re.Pattern, ...] = ()
    regexes: tuple[re.Pattern, ...] = ()
    stopwords: tuple[str, ...] = ()

    def allows_path(self, path: str) -> bool:
        return any(p.search(path) for p in self.paths)

    def allows_secret(self, secret: str) -> bool:
        lowered = secret.lower()
        return any(r.search(secret) for r in self.regexes) or any(w in lowered for w in self.stopwords)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int               # 0: the file itself (a path-only rule)
    rule: str
    description: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.rule} ({self.description})"


# --- the config --------------------------------------------------------------------------------

def _compile(pattern: str, what: str) -> re.Pattern:
    try:
        return re.compile(pattern, re.ASCII)       # RE2's \b, \w and \s are ASCII
    except re.error as exc:
        raise ScanError(f"{what}: the regex does not compile in Python: {exc}") from None


def load_config(path: Path) -> tuple[list[Rule], Allowlist]:
    try:
        data = load_toml(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ScanError(f"{path}: {exc}") from None
    rules = []
    for n, entry in enumerate(data.get("rules", []), 1):
        rule_id = entry.get("id") or f"rule {n}"
        if "regex" not in entry and "path" not in entry:
            raise ScanError(f"{path}: {rule_id} has neither a regex nor a path")
        regex = _compile(entry["regex"], f"{path}: {rule_id}") if "regex" in entry else None
        group = int(entry.get("secretGroup", 0))
        if regex is not None and group > regex.groups:
            raise ScanError(f"{path}: {rule_id}: secretGroup {group}, but the regex has {regex.groups} group(s)")
        rules.append(Rule(rule_id, entry.get("description", ""), regex, group,
                          _compile(entry["path"], f"{path}: {rule_id} path") if "path" in entry else None,
                          tuple(k.lower() for k in entry.get("keywords", []))))
    if not rules:
        raise ScanError(f"{path}: no [[rules]]")
    allow = data.get("allowlist", {})
    allowlist = Allowlist(tuple(_compile(p, f"{path}: allowlist path") for p in allow.get("paths", [])),
                          tuple(_compile(r, f"{path}: allowlist regex") for r in allow.get("regexes", [])),
                          tuple(w.lower() for w in allow.get("stopwords", [])))
    return rules, allowlist


def load_toml(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:                     # Python 3.10
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return parse_toml_subset(text)
    return tomllib.loads(text)


class _TomlSubset:
    """Tables, arrays of tables (with sub-tables of their last element), bare keys, and
    values that are strings (basic, literal, multi-line literal), integers, booleans or
    arrays of those. Anything else raises ValueError."""

    ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}

    def __init__(self, text: str):
        self.s, self.i = text, 0

    def fail(self, message: str):
        raise ValueError(f"line {self.s.count(chr(10), 0, self.i) + 1}: {message}")

    def skip(self, newlines: bool = False) -> None:
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t" or (newlines and c in "\r\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                return

    def name(self, closing: str) -> list[str]:
        end = self.s.find(closing, self.i)
        if end < 0:
            self.fail(f"no closing {closing}")
        parts = [p.strip() for p in self.s[self.i:end].split(".")]
        if not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
            self.fail("only bare table names are supported")
        self.i = end + len(closing)
        return parts

    def table(self, root: dict, parts: list[str]) -> tuple[dict, str]:
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if isinstance(node, list):
                node = node[-1]
            if not isinstance(node, dict):
                self.fail(f"{part} is not a table")
        return node, parts[-1]

    def value(self):
        s, i = self.s, self.i
        if i >= len(s):
            self.fail("a value is missing")
        if s.startswith("'''", i):
            end = s.find("'''", i + 3)
            if end < 0:
                self.fail("unterminated ''' string")
            text = s[i + 3:end]
            self.i = end + 3
            return text[1:] if text.startswith("\n") else text
        if s.startswith('"""', i):
            self.fail('""" strings are not supported')
        if s[i] == "'":
            end = s.find("'", i + 1)
            if end < 0 or "\n" in s[i:end]:
                self.fail("unterminated ' string")
            self.i = end + 1
            return s[i + 1:end]
        if s[i] == '"':
            out, j = [], i + 1
            while j < len(s) and s[j] != '"':
                if s[j] == "\n":
                    break
                if s[j] == "\\":
                    nxt = s[j + 1:j + 2]
                    if nxt in self.ESCAPES:
                        out.append(self.ESCAPES[nxt])
                        j += 2
                    elif nxt in ("u", "U"):
                        width = 4 if nxt == "u" else 8
                        out.append(chr(int(s[j + 2:j + 2 + width], 16)))
                        j += 2 + width
                    else:
                        self.i = j
                        self.fail("unknown escape")
                else:
                    out.append(s[j])
                    j += 1
            if j >= len(s) or s[j] != '"':
                self.fail('unterminated " string')
            self.i = j + 1
            return "".join(out)
        if s[i] == "[":
            self.i += 1
            items = []
            while True:
                self.skip(newlines=True)
                if self.s[self.i:self.i + 1] == "]":
                    self.i += 1
                    return items
                items.append(self.value())
                self.skip(newlines=True)
                if self.s[self.i:self.i + 1] == ",":
                    self.i += 1
                elif self.s[self.i:self.i + 1] != "]":
                    self.fail("expected , or ] in an array")
        m = re.compile(r"true|false|[+-]?[0-9][0-9_]*").match(s, i)
        if m is None:
            self.fail("unsupported value")
        self.i = m.end()
        word = m.group(0)
        return word == "true" if word in ("true", "false") else int(word.replace("_", ""))

    def parse(self) -> dict:
        root: dict = {}
        current = root
        while True:
            self.skip(newlines=True)
            if self.i >= len(self.s):
                return root
            if self.s.startswith("[[", self.i):
                self.i += 2
                parent, key = self.table(root, self.name("]]"))
                items = parent.setdefault(key, [])
                if not isinstance(items, list):
                    self.fail(f"{key} is not an array of tables")
                current = {}
                items.append(current)
            elif self.s[self.i] == "[":
                self.i += 1
                parent, key = self.table(root, self.name("]"))
                if key in parent:
                    self.fail(f"table {key} defined twice")
                current = parent[key] = {}
            else:
                m = re.compile(r"[A-Za-z0-9_-]+").match(self.s, self.i)
                if m is None:
                    self.fail("expected a key")
                key, self.i = m.group(0), m.end()
                self.skip()
                if self.s[self.i:self.i + 1] != "=":
                    self.fail("expected =")
                self.i += 1
                self.skip()
                if key in current:
                    self.fail(f"{key} defined twice")
                current[key] = self.value()
            self.skip()
            if self.i < len(self.s) and self.s[self.i] not in "\r\n":
                self.fail("expected the end of the line")


def parse_toml_subset(text: str) -> dict:
    return _TomlSubset(text).parse()


# --- scanning ------------------------------------------------------------------------------------

def scan_text(path: str, text: str, rules: list[Rule], allowlist: Allowlist) -> list[Finding]:
    """The findings in one file's text; ``path`` is relative to the repository, with '/'."""
    if allowlist.allows_path(path):
        return []
    lowered = text.lower()
    findings = []
    for rule in rules:
        if rule.path is not None and not rule.path.search(path):
            continue
        if rule.regex is None:
            findings.append(Finding(path, 0, rule.id, rule.description))
            continue
        if rule.keywords and not any(k in lowered for k in rule.keywords):
            continue
        for m in rule.regex.finditer(text):
            secret = (m.group(rule.secret_group) if rule.secret_group else m.group(0)) or ""
            if not secret or allowlist.allows_secret(secret):
                continue
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            if ALLOW_MARK in text[start:end if end >= 0 else len(text)]:
                continue
            findings.append(Finding(path, text.count("\n", 0, m.start()) + 1, rule.id, rule.description))
    return findings


def _git(root: Path, *args: str, data: Optional[bytes] = None) -> bytes:
    try:
        done = subprocess.run(["git", *args], cwd=root, input=data, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanError(f"git {args[0]}: {exc}") from None
    if done.returncode != 0:
        raise ScanError(f"git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def tracked_files(root: Path) -> Iterator[tuple[str, Optional[bytes]]]:
    """(path, content) of every file in the index, read from the work tree; None when the
    path is not a regular file there (deleted, a symlink, a submodule)."""
    for name in _git(root, "ls-files", "-z").decode("utf-8", errors="surrogateescape").split("\0"):
        if not name:
            continue
        full = root / name
        if full.is_symlink() or not full.is_file():
            yield name, None
        else:
            yield name, full.read_bytes()


def staged_files(root: Path) -> Iterator[tuple[str, Optional[bytes]]]:
    """(path, staged content) of every added, copied, modified or renamed file in the index."""
    names = [n for n in _git(root, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
             .decode("utf-8", errors="surrogateescape").split("\0") if n]
    if not names:
        return
    out = _git(root, "cat-file", "--batch",
               data="".join(f":{n}\n" for n in names).encode("utf-8", errors="surrogateescape"))
    pos = 0
    for name in names:
        eol = out.index(b"\n", pos)
        header = out[pos:eol]
        pos = eol + 1
        parts = header.rsplit(b" ", 2)              # "<oid> <type> <size>", or "<name> missing"
        if header.endswith(b" missing") or len(parts) != 3 or not parts[2].isdigit():
            yield name, None
            continue
        size = int(parts[2])
        body = out[pos:pos + size]
        pos += size + 1
        yield name, body if parts[1] == b"blob" else None


def given_files(root: Path, paths: Iterable[str]) -> Iterator[tuple[str, Optional[bytes]]]:
    for given in paths:
        full = Path(given).resolve()
        try:
            name = full.relative_to(root.resolve()).as_posix()
        except ValueError:
            name = Path(given).as_posix()
        if not full.is_file():
            raise ScanError(f"{given}: no such file")
        yield name, full.read_bytes()


def scan(files: Iterable[tuple[str, Optional[bytes]]], rules: list[Rule],
         allowlist: Allowlist) -> tuple[list[Finding], int]:
    """(findings, number of files read) over (path, content) pairs; binary content is skipped."""
    findings, read = [], 0
    for path, data in files:
        if data is None or b"\0" in data[:BINARY_PROBE]:
            continue
        read += 1
        findings.extend(scan_text(path, data.decode("utf-8", errors="replace"), rules, allowlist))
    return findings, read


def repo_root(start: Path) -> Path:
    return Path(_git(start, "rev-parse", "--show-toplevel").decode().strip())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("files", nargs="*", help="scan these files instead of the tracked ones")
    p.add_argument("--staged", action="store_true", help="scan the staged content of staged files")
    p.add_argument("--root", type=Path, default=None, help="repository root (default: git's top level)")
    p.add_argument("--config", type=Path, default=None, help=f"rules (default <root>/{CONFIG_NAME})")
    args = p.parse_args(argv)
    if args.staged and args.files:
        p.error("--staged scans the index; give no files with it")
    try:
        if args.root is not None:
            root = args.root.resolve()
        elif args.files:
            try:
                root = repo_root(Path.cwd())
            except ScanError:
                root = Path.cwd()
        else:
            root = repo_root(Path.cwd())
        rules, allowlist = load_config(args.config or root / CONFIG_NAME)
        if args.files:
            files = given_files(root, args.files)
        elif args.staged:
            files = staged_files(root)
        else:
            files = tracked_files(root)
        findings, read = scan(files, rules, allowlist)
    except ScanError as exc:
        print(f"secret_scan: {exc}", file=sys.stderr)
        return 2
    what = "staged file(s)" if args.staged else "file(s)"
    if not findings:
        print(f"secret_scan: nothing found in {read} {what} ({len(rules)} rules)")
        return 0
    for finding in findings:
        print(finding)
    print(f"secret_scan: {len(findings)} finding(s) in {read} {what} ({len(rules)} rules). Remove the secret "
          "(and rotate it if it was ever pushed), or mark a false positive with a gitleaks:allow comment.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
