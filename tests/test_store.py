"""The store (T3): one schema on SQLite and PostgreSQL, insert-only audit tables.

SQLite always runs. To run the same tests against PostgreSQL or TimescaleDB,
set PDM_TEST_PG to a scratch database, e.g.

    PDM_TEST_PG=postgresql://pdm:...@localhost:5432/pdm_test pytest tests/test_store.py

The tests drop and recreate the PdM tables in that database.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import numpy as np
import pytest

from pdm_common.profile import load_profile, signal_names
from pdm_common.store import INSERT_ONLY, SCHEMA, Store, StoreError, ddl, open_store

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ["sqlite"] + (["postgres"] if os.environ.get("PDM_TEST_PG") else [])


@pytest.fixture(params=ENGINES)
def store(request, tmp_path):
    if request.param == "sqlite":
        s = Store(f"sqlite:///{tmp_path}/pdm.db")
    else:
        s = Store(os.environ["PDM_TEST_PG"])
        for name in SCHEMA:
            s.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
    s.init_schema()
    yield s
    s.close()


def _tables(store: Store) -> set[str]:
    if store.dialect == "sqlite":
        rows = store.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    else:
        rows = store.query("SELECT table_name AS name FROM information_schema.tables WHERE table_schema = 'public'")
    return {r["name"] for r in rows}


def _minimal_row(table: str) -> dict:
    """A value for every column except the serial id."""
    sample = {"text": "x", "int": 1, "real": 1.5, "bool": True, "ts": "2020-04-18T00:00:00Z", "json": {"k": 1}}
    return {col: sample[kind] for col, kind in SCHEMA[table]["columns"] if kind != "serial"}


def test_schema_creates_every_table(store):
    assert set(SCHEMA) <= _tables(store)


def test_init_schema_is_idempotent(store):
    store.insert("incidents", {"incident_id": "I-1", "asset_id": "a", "state": "open"})
    store.init_schema()
    assert store.count("incidents") == 1


def test_round_trip_types(store):
    store.insert("scores", {
        "asset_id": "metropt3_apu", "window_start": dt.datetime(2020, 4, 17, 23, 50, tzinfo=dt.timezone.utc),
        "window_end": "2020-04-18T00:00:00Z", "score": np.float64(12.5),
        "errors": {"TP2_mean": np.float32(0.25), "H1_std": 1.0}, "threshold": 9.0, "alert": np.bool_(True),
        "detector_run_id": "bundle-abc", "profile_sha256": "f" * 64})
    row = store.select("scores")[0]
    assert row["window_start"] == "2020-04-17T23:50:00Z"
    assert row["window_end"] == "2020-04-18T00:00:00Z"
    assert row["errors"] == {"H1_std": 1.0, "TP2_mean": 0.25}
    assert row["alert"] is True and row["score"] == 12.5


def test_times_are_utc_and_ordered(store):
    # an offset in, UTC out; fractional seconds sort after whole ones
    for end in ("2020-04-18T02:00:00+02:00", "2020-04-18T00:00:00.5Z", "2020-04-18T00:01:00Z",
                "2020-04-17T23:59:00Z"):
        store.insert("windows", {"asset_id": "a", "window_end": end, "features": {}})
    ends = [r["window_end"] for r in store.select("windows", order="window_end")]
    assert ends == ["2020-04-17T23:59:00Z", "2020-04-18T00:00:00Z", "2020-04-18T00:00:00.500000Z",
                    "2020-04-18T00:01:00Z"]
    inside = store.between("windows", "window_end", "a", "2020-04-18T00:00:00Z", "2020-04-18T00:01:00Z",
                           end_inclusive=False)
    assert [r["window_end"] for r in inside] == ["2020-04-18T00:00:00Z", "2020-04-18T00:00:00.500000Z"]
    assert store.latest("windows", "window_end", "a", before="2020-04-18T00:00:59Z")["window_end"] == \
        "2020-04-18T00:00:00.500000Z"
    assert store.latest("windows", "window_end", "a")["window_end"] == "2020-04-18T00:01:00Z"


@pytest.mark.parametrize("table", INSERT_ONLY)
def test_database_refuses_changes_to_insert_only_tables(store, table):
    store.insert(table, _minimal_row(table))
    text_col = next(c for c, k in SCHEMA[table]["columns"] if k == "text")
    with pytest.raises(Exception, match="insert-only"):
        store.execute(f"UPDATE {table} SET {text_col} = 'changed'")
    with pytest.raises(Exception, match="insert-only"):
        store.execute(f"DELETE FROM {table}")
    assert store.count(table) == 1
    assert store.select(table)[0][text_col] == "x"


def test_update_is_for_mutable_tables_only(store):
    with pytest.raises(StoreError, match="insert-only"):
        store.update("decisions", {"decision_id": "D-1"}, {"reviewer": "someone"})
    store.insert("work_orders", {"wo_id": "W-1", "decision_id": "D-1", "status": "queued", "attempts": 0})
    store.update("work_orders", {"wo_id": "W-1"}, {"status": "sent", "attempts": 1,
                                                   "sent_at": "2020-04-18T01:00:00Z"})
    row = store.select("work_orders", {"wo_id": "W-1"})[0]
    assert (row["status"], row["attempts"], row["sent_at"]) == ("sent", 1, "2020-04-18T01:00:00Z")


def test_one_work_order_per_decision(store):
    store.insert("work_orders", {"wo_id": "W-1", "decision_id": "D-1", "status": "queued"})
    with pytest.raises(Exception):
        store.insert("work_orders", {"wo_id": "W-2", "decision_id": "D-1", "status": "queued"})
    store.insert("work_orders", {"wo_id": "W-2", "decision_id": "D-1", "status": "queued"}, ignore_conflict=True)
    assert store.count("work_orders") == 1


def test_transaction_commits_or_rolls_back_as_a_unit(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.insert("incidents", {"incident_id": "I-1", "asset_id": "a", "state": "open"})
            with store.transaction():                       # nested: joins the outer one
                store.insert("incident_events", {"incident_id": "I-1", "from_state": None,
                                                 "to_state": "open", "actor": "gate", "at": "2020-04-18T00:00:00Z"})
            raise RuntimeError("crash before commit")
    assert store.count("incidents") == 0 and store.count("incident_events") == 0
    with store.transaction():
        store.insert("incidents", {"incident_id": "I-1", "asset_id": "a", "state": "open"})
        store.insert("incident_events", {"incident_id": "I-1", "to_state": "open", "actor": "gate",
                                         "at": "2020-04-18T00:00:00Z"})
    assert store.count("incidents") == 1 and store.count("incident_events") == 1


def test_insert_many_skips_duplicates(store):
    rows = [{"asset_id": "a", "ts": f"2020-04-18T00:00:{s:02d}Z", "seq": s, "signals": {"x": s}} for s in range(0, 60, 10)]
    assert store.insert_many("telemetry", rows) == 6
    store.insert_many("telemetry", rows[:3])               # a replay restarted: no duplicates
    assert store.count("telemetry") == 6
    assert store.count("telemetry", {"asset_id": "a"}) == 6
    assert store.select("telemetry", {"ts": "2020-04-18T00:00:20Z"})[0]["signals"] == {"x": 20}


def test_serial_ids_increase(store):
    for code in ("bad_json", "missing_signal"):
        store.insert("quarantine", {"asset_id": "a", "received_at": "2020-04-18T00:00:00Z", "error_code": code})
    ids = [r["id"] for r in store.select("quarantine", order="id")]
    assert len(ids) == 2 and ids[0] < ids[1]


def test_checked_in_ddl_is_generated_from_the_schema():
    assert (ROOT / "db/init/001_schema.sql").read_text() == ddl("postgres"), \
        "regenerate: python -m pdm_common.store --ddl postgres > db/init/001_schema.sql"


def test_schema_names_no_asset_channel():
    """Signals and features are JSON, so the tables name no MetroPT channel (CFG-03)."""
    profile = load_profile(ROOT / "profiles/metropt3_apu/asset_profile.yaml")
    text = (ddl("postgres") + ddl("sqlite")).lower()
    for name in signal_names(profile):
        assert f" {name.lower()} " not in text and f"({name.lower()}" not in text, name


def test_open_store_uses_pdm_store(monkeypatch, tmp_path):
    monkeypatch.setenv("PDM_STORE", f"sqlite:///{tmp_path}/env.db")
    s = open_store()
    try:
        assert set(SCHEMA) <= _tables(s)
        assert (tmp_path / "env.db").exists()
    finally:
        s.close()


def test_unknown_url_is_refused():
    with pytest.raises(StoreError):
        Store("mysql://localhost/pdm")
