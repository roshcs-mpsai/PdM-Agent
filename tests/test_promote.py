"""Promotion gate (T25; OPS-07, PR-23): a recorded team approval, two people, no promotion
during the evaluation lock, every attempt on record in the insert-only promotions table.
MLflow is not needed: without it the approved path records that the alias was not moved,
and a stand-in client checks the path that moves it."""
import datetime as dt
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


promote = _load("promote", ROOT / "eval" / "promote.py")
from pdm_common.store import Store  # noqa: E402

MODEL = "pdm-detector-metropt3_apu"
TEAM = ["Rosh Sreedharan", "Ashok Reddy Buthukuri", "Gayatri Suresh Chaudhari"]
BEFORE_LOCK_ENDS = dt.date(2026, 10, 10)


@pytest.fixture()
def store():
    s = Store("sqlite://")
    s.init_schema()
    return s


@pytest.fixture()
def unlocked(tmp_path):
    """A copy of eval/config.yaml with the evaluation lock off."""
    config = yaml.safe_load((ROOT / "eval" / "config.yaml").read_text())
    config["evaluation_lock"] = {"locked": False, "until": "2026-11-22"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


@pytest.fixture(autouse=True)
def no_registry(monkeypatch):
    """Never reach a real registry from the tests, even where MLflow is installed."""
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)


def attempt(store, config, *, requested_by=TEAM[2], approved_by=TEAM[0], approval_ref="minutes 2026-11-24 item 3",
            today=BEFORE_LOCK_ENDS, client=None, version="3"):
    said = []
    code, outcome = promote.promote(MODEL, version, "production", requested_by, approved_by, approval_ref,
                                    store=store, config_path=config, today=today, client=client, say=said.append)
    return code, outcome, "\n".join(said)


class FakeClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def set_registered_model_alias(self, name, alias, version):
        if self.fail:
            raise RuntimeError(f"version {version} of {name} does not exist")
        self.calls.append((name, alias, version))


# --- refusals --------------------------------------------------------------------------------------

def test_the_evaluation_lock_refuses_until_its_date(store):
    config = ROOT / "eval" / "config.yaml"                 # the real freeze: locked until 2026-11-22
    code, outcome, said = attempt(store, config)
    assert code == 2 and outcome.startswith("refused: ") and "evaluation lock is on until 2026-11-22" in outcome
    assert "refused" in said
    code, outcome, _ = attempt(store, config, today=dt.date(2026, 11, 21))
    assert code == 2 and "evaluation lock" in outcome
    code, outcome, _ = attempt(store, config, today=dt.date(2026, 11, 22))   # the lock lapses on its date
    assert code == 0 and outcome == promote.NO_MLFLOW


def test_an_approver_outside_the_team_is_refused(store, unlocked):
    code, outcome, said = attempt(store, unlocked, approved_by="Youakim Badr")
    assert code == 2 and "'Youakim Badr' is not a team member" in outcome
    assert "not a team member" in said


@pytest.mark.parametrize("approver", ["Gayatri Suresh Chaudhari", "  gayatri   suresh chaudhari "])
def test_the_requester_cannot_approve(store, unlocked, approver):
    code, outcome, _ = attempt(store, unlocked, requested_by=TEAM[2], approved_by=approver)
    assert code == 2 and "two people are needed" in outcome


def test_another_spelling_of_the_requester_is_still_the_requester(store, unlocked):
    code, outcome, _ = attempt(store, unlocked, requested_by="Rosh Chathoth Sreedharan", approved_by="Rosh Sreedharan")
    assert code == 2 and "two people are needed" in outcome


@pytest.mark.parametrize("ref", ["", "   "])
def test_an_empty_approval_reference_is_refused(store, unlocked, ref):
    code, outcome, _ = attempt(store, unlocked, approval_ref=ref)
    assert code == 2 and "no approval reference" in outcome


def test_every_reason_is_reported_at_once(store):
    code, outcome, _ = attempt(store, ROOT / "eval" / "config.yaml", requested_by=TEAM[1], approved_by=TEAM[1],
                               approval_ref="")
    assert code == 2
    assert all(part in outcome for part in ("evaluation lock", "two people are needed", "no approval reference"))


# --- the permitted path -----------------------------------------------------------------------------

def test_an_approved_promotion_without_mlflow_is_recorded_and_prints_the_command(store, unlocked):
    code, outcome, said = attempt(store, unlocked)
    assert code == 0 and outcome == "approved: alias not moved (no MLflow here)"
    assert """set_registered_model_alias("pdm-detector-metropt3_apu", "production", "3")""" in said
    row = store.select("promotions")[0]
    assert (row["model_name"], row["version"], row["alias"], row["requested_by"], row["approved_by"],
            row["approval_ref"], row["outcome"]) == (MODEL, "3", "production", TEAM[2], TEAM[0],
                                                     "minutes 2026-11-24 item 3", promote.NO_MLFLOW)
    assert row["at"].endswith("Z")


def test_an_approved_promotion_moves_the_alias_through_mlflow(store, unlocked):
    client = FakeClient()
    code, outcome, said = attempt(store, unlocked, client=client)
    assert (code, outcome) == (0, "approved") and client.calls == [(MODEL, "production", "3")]
    assert "production -> pdm-detector-metropt3_apu version 3" in said
    assert [r["outcome"] for r in store.select("promotions", order="id")] == ["approved"]


def test_a_refused_promotion_never_reaches_mlflow(store):
    client = FakeClient()
    code, _, _ = attempt(store, ROOT / "eval" / "config.yaml", client=client)
    assert code == 2 and client.calls == []


def test_a_failed_alias_move_is_recorded_after_the_approval(store, unlocked):
    code, outcome, _ = attempt(store, unlocked, client=FakeClient(fail=True))
    assert code == 1 and outcome.startswith("failed: alias not moved (RuntimeError")
    assert [r["outcome"].split(" ")[0] for r in store.select("promotions", order="id")] == ["approved", "failed:"]


# --- the record --------------------------------------------------------------------------------------

def test_every_attempt_leaves_a_row_and_the_table_is_insert_only(store, unlocked):
    attempt(store, ROOT / "eval" / "config.yaml")
    attempt(store, unlocked, approved_by="nobody")
    attempt(store, unlocked, approved_by=TEAM[2])
    attempt(store, unlocked, approval_ref="")
    attempt(store, unlocked)
    outcomes = [r["outcome"] for r in store.select("promotions", order="id")]
    assert len(outcomes) == 5 and [o.split(":")[0] for o in outcomes] == ["refused"] * 4 + ["approved"]
    with pytest.raises(Exception, match="insert-only"):
        store.execute("UPDATE promotions SET outcome = 'approved'")
    with pytest.raises(Exception, match="insert-only"):
        store.execute("DELETE FROM promotions")
    assert store.count("promotions") == 5


def test_the_command_line_refuses_with_a_non_zero_exit(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'pdm.db'}"
    code = promote.main(["--model", MODEL, "--version", "3", "--alias", "production", "--requested-by", TEAM[0],
                         "--approved-by", TEAM[1], "--approval-ref", "PR #42", "--store", url])
    out = capsys.readouterr().out
    if dt.datetime.now(dt.timezone.utc).date() < dt.date(2026, 11, 22):
        assert code == 2 and "evaluation lock" in out
    rows = Store(url).select("promotions")
    assert len(rows) == 1 and rows[0]["approval_ref"] == "PR #42"


def test_the_approvers_are_the_three_team_members():
    assert sorted(set(promote.load_approvers().values())) == sorted(TEAM)


# --- PR-23: the pipeline cannot promote on its own ---------------------------------------------------------

def test_the_candidate_pipeline_has_no_way_to_move_an_alias(tmp_path):
    candidate = _load("retrain_candidate", ROOT / "eval" / "retrain_candidate.py")
    with pytest.raises(SystemExit):
        candidate.parse_args(["--processed", "x", "--trigger", "drift", "--alias", "production"])
    with pytest.raises(SystemExit, match="never goes under eval"):
        candidate.main(["--processed", str(tmp_path), "--trigger", "drift", "--out", str(ROOT / "eval" / "frozen")])
    for path in (ROOT / "eval" / "retrain_candidate.py", ROOT / "services" / "drift" / "drift.py"):
        assert "set_registered_model_alias" not in path.read_text(), path
    assert ".register(folder, experiment, name, None," in (ROOT / "eval" / "retrain_candidate.py").read_text()
