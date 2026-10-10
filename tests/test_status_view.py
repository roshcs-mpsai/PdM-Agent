"""Status view (R0.5): the board rows and the printed fallback, no broker needed.
The Streamlit page is now the four-page dashboard over the API (tests/test_dashboard.py)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "dashboard"))

import status_view  # noqa: E402
from pdm_common.profile import load_profile  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


def status(profile, ne107="good", score=1.25, **extra):
    return json.dumps(dict({
        "schema": "status_v1", "asset": profile["asset"]["id"], "ts": "2020-07-15T16:00:00Z",
        "score": score, "threshold": 4.0, "alert": ne107 != "good", "ne107": ne107,
        "detector_run": "zscore-0123456789ab", "profile_sha256": "x" * 64,
    }, **extra)).encode()


def test_no_status_yet_is_grey(profile):
    row = status_view.status_row(profile, None)
    assert row["marker"] == "grey"
    assert row["topic"] == profile["topics"]["status"]
    assert "no status yet" in status_view.format_row(row)


@pytest.mark.parametrize("ne107, colour", [
    ("good", "green"), ("out_of_specification", "amber"), ("maintenance_required", "amber"),
    ("function_check", "amber"), ("failure", "red"),
])
def test_marker_follows_the_ne107_state(profile, ne107, colour):
    row = status_view.status_row(profile, status(profile, ne107))
    assert (row["ne107"], row["marker"]) == (ne107, colour)
    assert row["ts"] == "2020-07-15T16:00:00Z"


def test_a_status_for_another_asset_is_not_shown_as_ours(profile):
    row = status_view.status_row(profile, status(profile, asset="another_asset"))
    assert row["marker"] == "amber" and row["ne107"] is None
    assert "unreadable" in row["note"]


def test_every_profile_is_discovered():
    assert PROFILE_PATH in status_view.discover_profiles()


def test_printed_fallback(profile, monkeypatch, capsys):
    """The log-line version of the status view, for the R0 gate if the page fails."""
    seen = []

    def fake_reader(host, port, timeout=1.5):
        def read(topic):
            seen.append(topic)
            return status(profile, "out_of_specification", score=5.39)
        return read

    monkeypatch.setattr(status_view, "mqtt_reader", fake_reader)
    status_view.main(["--profile", str(PROFILE_PATH)])
    line = capsys.readouterr().out.strip()
    assert seen == [profile["topics"]["status"]]
    assert line.startswith(profile["asset"]["id"])
    assert "[amber] out_of_specification" in line
    assert "last window 2020-07-15T16:00:00Z" in line and "score 5.39 / 4.00" in line
