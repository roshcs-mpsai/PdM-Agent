"""Broker accounts: passwords in .env, hashes in mosquitto/config/passwd (R3, NFR-06).

    python tools/broker_users.py            # a password in .env for every ACL account; write passwd
    python tools/broker_users.py --check    # every ACL account has a passwd entry, matching .env

The accounts are the ``user`` lines of mosquitto/config/acl. For each account, .env holds

    PDM_MQTT_USER_<KEY>=<account>
    PDM_MQTT_PASSWORD_<KEY>=<password>

where <KEY> is the account name as pdm_common.broker.env_key writes it (south-cip ->
SOUTH_CIP), which is where the services look (pdm_common.broker.client_options). A missing
password is generated with secrets.token_urlsafe(32), 256 random bits, and appended to .env.
A password set by hand needs at least 16 characters from A-Z a-z 0-9 . _ ~ + / = -, so that
docker compose and a shell read .env the same way. No password is ever printed.

.env and passwd are written with mode 0600 and must stay out of git: .env is git-ignored
already; mosquitto/config/passwd needs a .gitignore line (docs/SECURITY.md). The tool warns
when git does not ignore either file.

passwd has one line per account, in the format Mosquitto's mosquitto_passwd writes:

    <account>:$7$<iterations>$<salt>$<hash>

    $7$         hash type 7, PBKDF2-HMAC-SHA512 (type 6 is one salted SHA-512, not used)
    iterations  1000 unless --iterations says otherwise: the default of current
                mosquitto_passwd (Mosquitto 2.0's wrote 101)
    salt        12 random bytes, standard base64 (16 characters). Mosquitto 2.1 writes
                64-byte salts, which a 2.0 broker rejects; 2.1 reads both, so 12 it is
    hash        PBKDF2-HMAC-SHA512(password as UTF-8, salt, iterations), 64 bytes,
                standard base64 with padding (88 characters)

The broker runs one PBKDF2 per CONNECT, on its single thread: 1000 iterations take under a
millisecond. The generated passwords are random, so their strength does not rest on the
iteration count; raise it with --iterations for passwords chosen by hand.

An entry whose password and iteration count are unchanged keeps its salt, so a second run
writes the same file. Not checked here against a running broker or the real mosquitto_passwd
(neither is installed); tests/test_broker_security.py re-derives every entry with hashlib.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from pdm_common.broker import env_key  # noqa: E402

ACL = ROOT / "mosquitto" / "config" / "acl"
PASSWD = ROOT / "mosquitto" / "config" / "passwd"
ENV = ROOT / ".env"

ITERATIONS = 1000
SALT_BYTES = 12
HASH_BYTES = 64
MIN_PASSWORD = 16
ACCOUNT = re.compile(r"^[A-Za-z0-9_.-]+$")
PASSWORD = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
ENTRY = re.compile(r"^\$7\$([0-9]{1,9})\$([A-Za-z0-9+/]+={0,2})\$([A-Za-z0-9+/]+={0,2})$")
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
HEADER = "# MQTT broker accounts (tools/broker_users.py); the hashes are in mosquitto/config/passwd"


class UsersError(Exception):
    """Something the operator has to fix; the message never contains a password."""


# --- the ACL and the hash format -------------------------------------------------------------

def acl_users(path: Path) -> list[str]:
    """The account names, in the order of their ``user`` lines."""
    users: list[str] = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        words = line.split(None, 1)
        if not words or words[0] != "user":
            continue
        name = words[1].strip() if len(words) > 1 else ""
        if not ACCOUNT.match(name):
            raise UsersError(f"{path}:{n}: account name {name!r}: use letters, digits, '.', '_' or '-'")
        clash = next((u for u in users if env_key(u) == env_key(name)), None)
        if clash == name:
            raise UsersError(f"{path}:{n}: account {name} appears twice")
        if clash is not None:
            raise UsersError(f"{path}:{n}: accounts {clash} and {name} would share the "
                             f"PDM_MQTT_*_{env_key(name)} variables")
        users.append(name)
    if not users:
        raise UsersError(f"{path}: no 'user' lines")
    return users


def hash_password(password: str, iterations: int = ITERATIONS, salt: Optional[bytes] = None) -> str:
    """The $7$ string mosquitto_passwd would write for ``password`` (a new random salt by default)."""
    salt = os.urandom(SALT_BYTES) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations, HASH_BYTES)
    return f"$7${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def parse_hash(text: str) -> Optional[tuple[int, bytes, bytes]]:
    """(iterations, salt, digest) of a $7$ string a Mosquitto 2.x broker accepts, else None."""
    m = ENTRY.match(text)
    if m is None:
        return None
    try:
        salt = base64.b64decode(m.group(2), validate=True)
        digest = base64.b64decode(m.group(3), validate=True)
    except (binascii.Error, ValueError):
        return None
    iterations = int(m.group(1))
    if iterations < 1 or len(salt) not in (SALT_BYTES, 64) or len(digest) != HASH_BYTES:
        return None
    return iterations, salt, digest


def verify(password: str, text: str) -> bool:
    parsed = parse_hash(text)
    if parsed is None:
        return False
    iterations, salt, digest = parsed
    got = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations, HASH_BYTES)
    return hmac.compare_digest(got, digest)


def read_passwd(path: Path) -> dict[str, str]:
    """account -> hash string, from a Mosquitto password file."""
    entries: dict[str, str] = {}
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise UsersError(f"{path}:{n}: not an 'account:hash' line")
        entries[name] = value.strip()
    return entries


# --- .env ------------------------------------------------------------------------------------

def read_env(path: Path) -> tuple[str, dict[str, str]]:
    """The file's text and its KEY=VALUE pairs, read the way docker compose reads .env:
    comments and blank lines skipped, 'export ' allowed, quotes around a value removed,
    ' #' after an unquoted value starts a comment. A later line wins."""
    text = Path(path).read_text() if Path(path).exists() else ""
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = ENV_LINE.match(line)
        if m is None or line.lstrip().startswith("#"):
            continue
        value = line[m.end():].strip()
        end = value.find(value[0], 1) if value[:1] in ("'", '"') else -1
        values[m.group(1)] = value[1:end] if end > 0 else value.split(" #", 1)[0].rstrip()
    return text, values


def _env_vars(user: str) -> tuple[str, str]:
    key = env_key(user)
    return f"PDM_MQTT_USER_{key}", f"PDM_MQTT_PASSWORD_{key}"


def ensure_passwords(users: list[str], env_path: Path) -> tuple[dict[str, str], list[str]]:
    """Every account's password from .env, generating and appending the missing ones.
    Returns (account -> password, accounts whose password was generated)."""
    text, values = read_env(env_path)
    passwords: dict[str, str] = {}
    generated: list[str] = []
    fill: dict[str, str] = {}                    # variables to (re)write: absent or empty
    for user in users:
        user_var, password_var = _env_vars(user)
        named = values.get(user_var, "")
        if named and named != user:
            raise UsersError(f"{env_path}: {user_var} is {named!r}, but the ACL account is {user!r}")
        if not named:
            fill[user_var] = user
        password = values.get(password_var, "")
        if password:
            if len(password) < MIN_PASSWORD or not PASSWORD.match(password):
                raise UsersError(f"{env_path}: {password_var} needs at least {MIN_PASSWORD} characters from "
                                 "A-Z a-z 0-9 . _ ~ + / = - (delete the line to have one generated)")
        else:
            password = secrets.token_urlsafe(32)
            fill[password_var] = password
            generated.append(user)
        passwords[user] = password
    if fill:
        kept = [line for line in text.splitlines()
                if (m := ENV_LINE.match(line)) is None or line.lstrip().startswith("#") or m.group(1) not in fill]
        block = ([HEADER] if HEADER not in kept else []) + [f"{k}={v}" for k, v in fill.items()]
        _write_private(env_path, "\n".join(kept + block) + "\n")
    elif Path(env_path).exists() and Path(env_path).stat().st_mode & 0o077:
        print(f"note: {env_path} is readable by other users; chmod 600 it", file=sys.stderr)
    return passwords, generated


# --- files ------------------------------------------------------------------------------------

def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 0600, atomically (a temp file in the same folder)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")      # created 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _warn_if_tracked(path: Path) -> None:
    """A warning when ``path`` is inside a git work tree and git does not ignore it."""
    path = Path(path).resolve()
    try:
        inside = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, timeout=10)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return
        ignored = subprocess.run(["git", "-C", str(path.parent), "check-ignore", "-q", path.name],
                                 capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return
    if ignored.returncode == 1:
        print(f"warning: git does not ignore {path}; add it to .gitignore before committing anything",
              file=sys.stderr)


def write_passwd(users: list[str], passwords: dict[str, str], passwd_path: Path,
                 iterations: int = ITERATIONS) -> tuple[int, int, int]:
    """Write one entry per account. Returns (kept, new, dropped): entries kept as they were,
    entries hashed now, and entries of accounts no longer in the ACL."""
    old = read_passwd(passwd_path) if Path(passwd_path).exists() else {}
    lines, kept, new = [], 0, 0
    for user in users:
        entry = old.get(user, "")
        parsed = parse_hash(entry)
        if (parsed is not None and parsed[0] == iterations and len(parsed[1]) == SALT_BYTES
                and verify(passwords[user], entry)):
            kept += 1
        else:
            entry = hash_password(passwords[user], iterations)
            new += 1
        lines.append(f"{user}:{entry}")
    _write_private(passwd_path, "\n".join(lines) + "\n")
    return kept, new, len(set(old) - set(users))


def check(users: list[str], passwd_path: Path, env_path: Path) -> list[str]:
    """Problems with the password file: missing, malformed or extra entries, and entries
    that do not match the password in .env. An empty list means all is well."""
    if not Path(passwd_path).exists():
        return [f"{passwd_path} does not exist: run python tools/broker_users.py"]
    entries = read_passwd(passwd_path)
    _, values = read_env(env_path)
    problems = []
    for user in users:
        entry = entries.get(user)
        if entry is None:
            problems.append(f"{user}: no entry in {passwd_path}")
        elif parse_hash(entry) is None:
            problems.append(f"{user}: the entry in {passwd_path} is not a $7$ hash Mosquitto 2.x accepts")
        else:
            password = values.get(_env_vars(user)[1])
            if password and not verify(password, entry):
                problems.append(f"{user}: the entry in {passwd_path} does not match "
                                f"{_env_vars(user)[1]} in {env_path}; run python tools/broker_users.py")
    for extra in sorted(set(entries) - set(users)):
        problems.append(f"{extra}: has a password entry but no 'user' line in the ACL; remove it")
    return problems


# --- command line -----------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--acl", type=Path, default=ACL, help=f"Mosquitto ACL file (default {ACL.relative_to(ROOT)})")
    p.add_argument("--env", type=Path, default=ENV, help="the .env with the passwords (default .env)")
    p.add_argument("--passwd", type=Path, default=PASSWD,
                   help=f"the password file to write (default {PASSWD.relative_to(ROOT)})")
    p.add_argument("--iterations", type=int, default=ITERATIONS, help=f"PBKDF2 iterations (default {ITERATIONS})")
    p.add_argument("--check", action="store_true",
                   help="write nothing; exit 1 unless every ACL account has a valid entry that matches .env")
    args = p.parse_args(argv)
    if not 1 <= args.iterations <= 10_000_000:
        p.error("--iterations must be between 1 and 10,000,000")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        users = acl_users(args.acl)
        if args.check:
            problems = check(users, args.passwd, args.env)
            for problem in problems:
                print(problem)
            if problems:
                return 1
            print(f"ok: all {len(users)} accounts of {args.acl} have a valid entry in {args.passwd}")
            return 0
        passwords, generated = ensure_passwords(users, args.env)
        kept, new, dropped = write_passwd(users, passwords, args.passwd, args.iterations)
    except (UsersError, OSError) as exc:
        print(f"broker_users: {exc}", file=sys.stderr)
        return 2
    for path in (args.env, args.passwd):
        _warn_if_tracked(path)
    removed = f", {dropped} removed" if dropped else ""
    source = (f"{len(generated)} new password(s) written to {args.env}" if generated
              else f"passwords from {args.env}")
    print(f"{args.passwd}: {len(users)} accounts ({kept} unchanged, {new} hashed now{removed}); {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
