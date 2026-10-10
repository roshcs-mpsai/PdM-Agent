"""Secrets out of git (T2, build guide R3.2; NFR-06): .gitleaks.toml as tools/secret_scan.py
applies it.

Every planted secret here is assembled when the test runs, from a seeded generator, so this
file holds none (it is scanned with the rest of the repository) and a run cannot fail by
chance.
"""
import os
import random
import shutil
import string
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import broker_users  # noqa: E402
import secret_scan  # noqa: E402

CONFIG = ROOT / ".gitleaks.toml"
# T2's files, scanned even before they are committed
THIS_TASK = [".gitleaks.toml", "docs/SECURITY.md", "lib/pdm_common/broker.py", "lib/pdm_common/mqtt.py",
             "mosquitto/config/acl", "mosquitto/config/mosquitto.secure.conf", "tests/test_broker_security.py",
             "tests/test_secrets.py", "tools/broker_users.py", "tools/make_certs.sh", "tools/secret_scan.py"]
ALNUM = string.ascii_letters + string.digits


def token(n: int, seed: int, alphabet: str = ALNUM) -> str:
    return "".join(random.Random(seed).choices(alphabet, k=n))


def planted() -> list:
    """(file name, line, the rule that must flag it), at least one per regex rule. In a
    YAML or env file secret-in-config may flag the same line as well."""
    return [
        ("passwd.txt", "svc:" + broker_users.hash_password("pw-" + token(20, 0), 1000, bytes(range(12))),
         "mosquitto-password-hash"),
        ("notes.txt", "key " + "sk-" + "ant-api03-" + token(93, 1, ALNUM + "_-") + "AA", "anthropic-api-key"),
        ("notes.txt", "OPENAI=" + "sk-" + "proj-" + token(40, 2), "openai-api-key"),
        ("notes.txt", "legacy " + "sk-" + token(48, 3), "openai-api-key"),
        ("notes.txt", "MLFLOW_TRACKING_" + "TOKEN=" + token(24, 4), "mlflow-tracking-credential"),
        ("notes.txt", "host " + "dapi" + token(32, 5, "0123456789abcdef"), "databricks-token"),
        ("notes.txt", "curl -H 'Authorization: " + "Bearer " + token(40, 6) + "'", "bearer-token"),
        ("settings.yaml", "api_" + "token: " + token(24, 7), "secret-in-config"),
        ("service.env", "WORKORDER_SIM_" + "TOKEN=" + token(24, 8), "secret-in-config"),
        ("notes.txt", "PDM_TOKEN_" + "REVIEWER=" + token(32, 9), "pdm-secret-variable"),
        ("notes.txt", "PDM_MQTT_PASSWORD_" + "GATE=" + token(43, 10), "pdm-secret-variable"),
        ("notes.txt", "PG_" + "PASSWORD=" + "hunter2", "postgres-password"),
        ("compose.yaml", "  POSTGRES_" + "PASSWORD: ${PG_" + "PASSWORD:-" + "hunter2}", "postgres-password"),
        ("notes.txt", "PDM_STORE=postgresql://pdm:" + "hunter2" + "@db:5432/pdm", "url-credentials"),
        ("notes.txt", "-----BEGIN " + "EC PRIVATE KEY-----", "private-key"),
    ]


# Placeholders and references as they appear in this repository: none is a secret.
PLACEHOLDERS = [
    (".env.example", "PG_PASSWORD=change-me"),
    (".env.example", "# PDM_STORE=postgresql://pdm:change-me@localhost:5432/pdm"),
    (".env.example", "PDM_STORE=postgresql://pdm:${PG_PASSWORD}@postgres:5432/pdm"),
    ("compose.yaml", "POSTGRES_PASSWORD: ${PG_PASSWORD:?set PG_PASSWORD in .env}"),
    ("compose.yaml", "PDM_MQTT_PASSWORD: ${PDM_MQTT_PASSWORD_INGEST:?run python tools/broker_users.py}"),
    ("ci.yml", "token: ${{ secrets.GITHUB_TOKEN }}"),
    ("README.md", 'curl -H "Authorization: Bearer sim-token" localhost:8088'),
    ("README.md", "Authorization: Bearer <token>"),
    ("README.md", 'export PDM_STORE="postgresql://pdm:<password>@localhost:5432/pdm"'),
    ("store.py", "Store('postgresql://pdm:...@localhost/pdm')"),
    ("tool.py", "--store postgresql://pdm:$PG_PASSWORD@localhost:5432/pdm"),
    ("broker.py", 'password = os.environ.get("PDM_MQTT_PASSWORD")'),
    ("SECURITY.md", "PDM_MQTT_PASSWORD_INGEST=<generated>"),
    ("mosquitto.conf", "password_file /mosquitto/config/passwd"),
    ("config.yaml", "max_tokens: 16000"),
    ("SECURITY.md", "<account>:$7$<iterations>$<salt>$<hash>"),
    ("notes.md", "OPENAI_API_KEY=" + "sk-proj-" + "x" * 24),
    ("notes.md", "the key looks like sk-ant-api03-..."),
]


@pytest.fixture(scope="module")
def config():
    return secret_scan.load_config(CONFIG)


def kinds_of(findings):
    return sorted((f.path, f.line, f.rule) for f in findings)


# --- the rules ---------------------------------------------------------------------------------

def test_every_planted_secret_is_found_by_its_rule(config):
    rules, allowlist = config
    for name, line, rule in planted():
        found = secret_scan.scan_text(name, "first line\n" + line + "\n", rules, allowlist)
        also = {"secret-in-config"} if name.endswith((".yaml", ".env")) else set()
        flagged_by = {f.rule for f in found}
        assert {f.line for f in found} == {2} and rule in flagged_by and flagged_by <= {rule} | also, \
            (rule, kinds_of(found))


def test_every_regex_rule_has_a_planted_secret(config):
    rules, _ = config
    assert {r.id for r in rules if r.regex is not None} == {rule for _, _, rule in planted()}


@pytest.mark.parametrize("name, line", PLACEHOLDERS)
def test_placeholders_are_not_secrets(config, name, line):
    rules, allowlist = config
    assert secret_scan.scan_text(name, line + "\n", rules, allowlist) == []


def test_secret_files_are_flagged_by_name(config):
    rules, allowlist = config
    for name in (".env", "deploy/.env", "mosquitto/config/passwd", "mosquitto/certs/server.key", "keystore.p12"):
        assert kinds_of(secret_scan.scan_text(name, "x\n", rules, allowlist)) == [(name, 0, "secret-file")], name
    for name in (".env.example", "docs/env.md", "mosquitto/config/acl", "server.crt"):
        assert secret_scan.scan_text(name, "x\n", rules, allowlist) == [], name


def test_allow_comments_fixtures_and_binary_files(config):
    rules, allowlist = config
    name, line, _ = planted()[9]
    assert secret_scan.scan_text(name, line + "  # gitleaks:allow\n", rules, allowlist) == []
    assert secret_scan.scan_text("tests/fixtures/sample.txt", line + "\n", rules, allowlist) == []
    findings, read = secret_scan.scan([("blob.bin", b"\0\1\2" + line.encode()), ("text.txt", line.encode())],
                                      rules, allowlist)
    assert read == 1 and kinds_of(findings) == [("text.txt", 1, "pdm-secret-variable")]


def test_the_broker_users_output_would_be_caught(config, tmp_path, capsys):
    rules, allowlist = config
    env, passwd = tmp_path / ".env", tmp_path / "passwd"
    assert broker_users.main(["--env", str(env), "--passwd", str(passwd)]) == 0
    capsys.readouterr()
    users = broker_users.acl_users(ROOT / "mosquitto" / "config" / "acl")
    hashes = secret_scan.scan_text("passwd.txt", passwd.read_text(), rules, allowlist)
    assert [f.rule for f in hashes] == ["mosquitto-password-hash"] * len(users)
    lines = secret_scan.scan_text(".env", env.read_text(), rules, allowlist)
    assert sorted(f.rule for f in lines) == sorted(["secret-file"] + ["pdm-secret-variable"] * len(users))


def test_the_regexes_stay_within_what_re2_and_python_share():
    """gitleaks runs Go's RE2, the scanner Python's re: no look-arounds, back-references,
    possessive quantifiers or \\Z / \\z, and (?i) only at the start."""
    data = secret_scan.load_toml(CONFIG.read_text())
    patterns = [r[k] for r in data["rules"] for k in ("regex", "path") if k in r]
    patterns += data["allowlist"]["paths"] + data["allowlist"]["regexes"]
    for p in patterns:
        for bad in ("(?=", "(?!", "(?<=", "(?<!", "(?P=", "*+", "++", "?+", "\\Z", "\\z"):
            assert bad not in p, (p, bad)
        assert not any(f"\\{d}" in p for d in "123456789"), p
        assert "(?i)" not in p[1:], p


# --- the config reader ----------------------------------------------------------------------------

def test_the_fallback_toml_reader_matches_tomllib():
    tomllib = pytest.importorskip("tomllib")
    text = CONFIG.read_text()
    assert secret_scan.parse_toml_subset(text) == tomllib.loads(text)
    sample = ('# c\ntitle = "a \\"q\\" \\u00e9"\n[t]\nn = 1_000\nok = false\n'
              "[[r]]\nx = '''lit\\eral'''\nys = [\n  'a', # c\n  \"b\",\n]\n[r.sub]\nz = -2\n[[r]]\nx = ''\n")
    assert secret_scan.parse_toml_subset(sample) == tomllib.loads(sample)


@pytest.mark.parametrize("text", ['a = """x"""\n', "a = 1.5\n", "a = {b = 1}\n", "a.b = 1\n", "a = 1\na = 2\n",
                                  "a = 'x' b = 1\n", "a = [1, 2\n", "a =", "[t]\n[t]\n", "a = 'x\n"])
def test_the_fallback_toml_reader_refuses_what_it_does_not_know(text):
    with pytest.raises(ValueError):
        secret_scan.parse_toml_subset(text)


# --- the command line, in a scratch git repository ------------------------------------------------

def git(repo: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, HOME=str(repo))
    subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)                       # never touch the repository pytest runs in
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    return path


def run_scan(capsys, *argv):
    code = secret_scan.main(["--config", str(CONFIG), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_tracked_and_staged_scans_in_a_git_repository(repo, capsys):
    name, line, _ = planted()[7]                           # a token in a YAML file
    secret = line.split(": ", 1)[1]
    (repo / name).write_text("service: x\n" + line + "\n")
    (repo / "README.md").write_text("nothing to see\n")
    (repo / "untracked.txt").write_text(planted()[9][1] + "\n")
    git(repo, "add", name, "README.md")

    code, out, err = run_scan(capsys, "--root", str(repo))
    assert code == 1 and f"{name}:2: secret-in-config" in out and "untracked.txt" not in out
    assert secret not in out + err                         # the secret is never printed
    code, out, _ = run_scan(capsys, "--root", str(repo), "--staged")
    assert code == 1 and f"{name}:2: secret-in-config" in out

    (repo / name).write_text("service: x\n")               # cleaned in the work tree, not staged
    assert run_scan(capsys, "--root", str(repo))[0] == 0
    assert run_scan(capsys, "--root", str(repo), "--staged")[0] == 1    # the commit would still carry it
    git(repo, "add", name)
    assert run_scan(capsys, "--root", str(repo), "--staged")[0] == 0

    (repo / ".env").write_text("A=1\n")
    git(repo, "add", "-f", ".env")
    code, out, _ = run_scan(capsys, "--root", str(repo), "--staged")
    assert code == 1 and ".env: secret-file" in out


def test_given_files_and_errors(tmp_path, capsys):
    clean, dirty = tmp_path / "clean.txt", tmp_path / "dirty.txt"
    clean.write_text("nothing\n")
    dirty.write_text(planted()[11][1] + "\n")
    assert run_scan(capsys, "--root", str(tmp_path), str(clean))[0] == 0
    code, out, _ = run_scan(capsys, "--root", str(tmp_path), str(clean), str(dirty))
    assert code == 1 and "dirty.txt:1: postgres-password" in out and "hunter2" not in out
    assert run_scan(capsys, "--root", str(tmp_path), str(tmp_path / "missing.txt"))[0] == 2
    bad = tmp_path / "bad.toml"
    bad.write_text("[[rules]]\nid = 'broken'\nregex = '''(unclosed'''\n")
    code, _, err = run_scan(capsys, "--root", str(tmp_path), "--config", str(bad), str(clean))
    assert code == 2 and "broken: the regex does not compile" in err


# --- this repository --------------------------------------------------------------------------------

def test_the_repository_is_clean(config):
    """Every tracked file, plus this task's files whether or not they are committed yet."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    rules, allowlist = config
    files = list(secret_scan.tracked_files(ROOT))
    files += list(secret_scan.given_files(ROOT, [str(ROOT / p) for p in THIS_TASK]))
    findings, read = secret_scan.scan(files, rules, allowlist)
    assert [str(f) for f in findings] == []
    assert read > 100
