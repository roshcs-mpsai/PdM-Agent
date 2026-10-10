"""The simulated work-order system (T31; the PR-19 test bed for INT-04 to INT-06).

Every answer of the contract the REST connector is built against -- 201, 200,
409, 401, 422, 503 and then success -- through starlette's TestClient, with no
server and no network. Skipped where starlette or httpx is not installed.
"""
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="starlette's TestClient needs httpx")
TestClient = pytest.importorskip("starlette.testclient").TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import workorder_sim  # noqa: E402

URL = "/api/work-orders"
TOKEN = "test-token"
NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
APPROVED_TEXT = "Inspect the air circuit for a leak and check the drain valves.\n"


def order(**changes) -> dict:
    body = {
        "approval_id": "APR-0001", "wo_id": "WO-0001", "incident_id": "INC-test_asset-20200715T0030Z",
        "asset_id": "test_asset", "title": "Air leak suspected", "priority": "high",
        "tasks": ["Inspect the air circuit", "Check the drain valves"], "safety_notes": ["Isolate and vent first"],
        "text_sha256": hashlib.sha256(APPROVED_TEXT.encode()).hexdigest(),
        "approved_by": "planner@example.com", "approved_at": "2026-10-10T11:58:00Z", "brief_id": "BRF-0001",
    }
    body.update(changes)
    return body


def headers(key="APR-0001", token=TOKEN) -> dict:
    h = {}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    if key is not None:
        h["Idempotency-Key"] = key
    return h


def app_client(**kwargs) -> TestClient:
    kwargs.setdefault("token", TOKEN)
    kwargs.setdefault("fail_first", 0)
    return TestClient(workorder_sim.create_app(clock=lambda: NOW, **kwargs))


def listed(client) -> list:
    response = client.get(URL, headers=headers(key=None))
    assert response.status_code == 200
    return response.json()["orders"]


def test_health_needs_no_token():
    response = app_client().get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_the_first_post_creates_an_order():
    client = app_client()
    response = client.post(URL, json=order(), headers=headers())
    assert response.status_code == 201
    assert response.json() == {"reference": "WO-SIM-0001", "received_at": "2026-10-10T12:00:00Z"}
    second = client.post(URL, json=order(approval_id="APR-0002", wo_id="WO-0002"), headers=headers("APR-0002"))
    assert second.status_code == 201 and second.json()["reference"] == "WO-SIM-0002"
    got = listed(client)
    assert [o["reference"] for o in got] == ["WO-SIM-0001", "WO-SIM-0002"]
    assert got[0] == {"reference": "WO-SIM-0001", "received_at": "2026-10-10T12:00:00Z", "approval_id": "APR-0001",
                      "order": order()}                           # extra fields (brief_id) kept as sent


def test_the_same_key_and_body_again_is_the_same_order():
    """A retry after a lost response: 200, the same reference, one order."""
    client = app_client()
    first = client.post(URL, json=order(), headers=headers())
    again = client.post(URL, json=order(), headers=headers())
    reordered = json.dumps(dict(reversed(list(order().items()))), indent=2)        # same content, other bytes
    third = client.post(URL, content=reordered, headers={**headers(), "Content-Type": "application/json"})
    assert first.status_code == 201
    for response in (again, third):
        assert response.status_code == 200 and response.json() == first.json()
        assert response.headers["Idempotent-Replayed"] == "true"
    assert len(listed(client)) == 1


def test_the_same_key_with_another_body_is_a_conflict():
    client = app_client()
    client.post(URL, json=order(), headers=headers())
    tampered = order(tasks=["Replace the compressor"])
    response = client.post(URL, json=tampered, headers=headers())
    assert response.status_code == 409 and response.json()["reference"] == "WO-SIM-0001"
    assert [o["order"] for o in listed(client)] == [order()]


@pytest.mark.parametrize("auth", [None, "Bearer wrong-token", "Bearer ", "Basic dGVzdC10b2tlbg==", f"bearer{TOKEN}"])
def test_a_missing_or_wrong_token_is_refused(auth):
    client = app_client()
    h = headers(token=None)
    if auth is not None:
        h["Authorization"] = auth
    response = client.post(URL, json=order(), headers=h)
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"
    assert client.get(URL, headers={} if auth is None else {"Authorization": auth}).status_code == 401
    assert listed(client) == []


def test_the_scheme_is_case_insensitive():
    client = app_client()
    response = client.post(URL, json=order(), headers={**headers(token=None), "Authorization": f"bearer {TOKEN}"})
    assert response.status_code == 201


@pytest.mark.parametrize("changes, problem", [
    ({"title": ""}, "title"),
    ({"priority": 3}, "priority"),
    ({"approval_id": 1}, "approval_id"),
    ({"tasks": "inspect"}, "tasks"),
    ({"safety_notes": ["ok", 2]}, "safety_notes"),
    ({"text_sha256": "ABC"}, "text_sha256"),
    ({"text_sha256": hashlib.sha256(b"x").hexdigest().upper()}, "text_sha256"),
    ({"approved_at": "2026-10-10T11:58:00"}, "approved_at"),            # no offset
    ({"approved_at": "yesterday"}, "approved_at"),
    ({"approved_at": 20261010}, "approved_at"),
])
def test_a_mistyped_field_is_unprocessable(changes, problem):
    client = app_client()
    response = client.post(URL, json=order(**changes), headers=headers(key=str(changes.get("approval_id", "APR-0001"))))
    assert response.status_code == 422
    assert any(p.startswith(problem) for p in response.json()["problems"])
    assert listed(client) == []


@pytest.mark.parametrize("field", ["approval_id", "wo_id", "incident_id", "asset_id", "title", "priority", "tasks",
                                   "safety_notes", "text_sha256", "approved_by", "approved_at"])
def test_a_missing_field_is_unprocessable(field):
    client = app_client()
    body = {k: v for k, v in order().items() if k != field}
    response = client.post(URL, json=body, headers=headers())
    assert response.status_code == 422 and f"{field}: missing" in response.json()["problems"]


def test_other_unprocessable_requests():
    client = app_client()
    assert client.post(URL, json=order(), headers=headers(key=None)).status_code == 422          # no key
    assert client.post(URL, json=order(), headers=headers(key="  ")).status_code == 422
    mismatch = client.post(URL, json=order(), headers=headers(key="APR-9999"))
    assert mismatch.status_code == 422 and "approval_id" in mismatch.json()["error"]
    not_json = client.post(URL, content=b"{not json", headers={**headers(), "Content-Type": "application/json"})
    assert not_json.status_code == 422
    assert client.post(URL, json=[order()], headers=headers()).status_code == 422                 # not an object
    assert client.post(URL, content=b"", headers=headers()).status_code == 422
    assert listed(client) == []


def test_injected_failures_then_success():
    """The connector's retry test: the first N POSTs fail with 503 and store
    nothing, whatever they carry; the retry then succeeds once."""
    client = app_client(fail_first=3)
    assert client.post(URL, json=order(), headers=headers()).status_code == 503
    assert client.post(URL, json=order(), headers=headers(token=None)).status_code == 503       # before the token check
    assert listed(client) == []
    assert client.post(URL, json=order(), headers=headers()).status_code == 503
    response = client.post(URL, json=order(), headers=headers())
    assert response.status_code == 201 and response.json()["reference"] == "WO-SIM-0001"
    assert client.post(URL, json=order(), headers=headers()).status_code == 200
    assert len(listed(client)) == 1


def test_configuration_from_the_environment(monkeypatch):
    monkeypatch.setenv("WORKORDER_SIM_TOKEN", "from-env")
    monkeypatch.setenv("WORKORDER_SIM_FAIL_FIRST", "1")
    monkeypatch.delenv("WORKORDER_SIM_STORE", raising=False)
    client = TestClient(workorder_sim.create_app())
    assert client.post(URL, json=order(), headers=headers(token="from-env")).status_code == 503
    assert client.post(URL, json=order(), headers=headers(token=TOKEN)).status_code == 401
    assert client.post(URL, json=order(), headers=headers(token="from-env")).status_code == 201
    monkeypatch.delenv("WORKORDER_SIM_TOKEN")
    monkeypatch.delenv("WORKORDER_SIM_FAIL_FIRST")
    default = TestClient(workorder_sim.create_app())
    assert default.post(URL, json=order(), headers=headers(token="sim-token")).status_code == 201
    with pytest.raises(ValueError):
        workorder_sim.create_app(token="")


def test_orders_survive_a_restart_with_a_store(tmp_path):
    store = tmp_path / "orders.json"
    first = app_client(store_path=store)
    assert first.post(URL, json=order(), headers=headers()).status_code == 201
    restarted = app_client(store_path=store)
    again = restarted.post(URL, json=order(), headers=headers())
    assert again.status_code == 200 and again.json()["reference"] == "WO-SIM-0001"
    assert restarted.post(URL, json=order(tasks=[]), headers=headers()).status_code == 409
    other = restarted.post(URL, json=order(approval_id="APR-0002"), headers=headers("APR-0002"))
    assert other.json()["reference"] == "WO-SIM-0002"
    assert [o["approval_id"] for o in json.loads(store.read_text())["orders"]] == ["APR-0001", "APR-0002"]
