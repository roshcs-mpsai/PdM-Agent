"""Status view (R0.5): the board rows, the printed fallback, and the
Streamlit page run against a stand-in streamlit module (the real one is a
dashboard-only dependency). No broker needed."""
import json
import runpy
import sys
import types
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


class FakeStreamlit(types.ModuleType):
    """Just enough of streamlit to execute app.py and record what it draws."""

    def __init__(self):
        super().__init__("streamlit")
        self.drawn = []

    def _record(self, *args, **kwargs):
        self.drawn.append(" ".join(str(a) for a in args))

    set_page_config = title = caption = error = button = markdown = _record

    def columns(self, spec):
        return [self for _ in spec]

    def cache_resource(self, func):
        return func

    def fragment(self, func=None, *, run_every=None):
        self.run_every = run_every
        return lambda f: f


def test_streamlit_page_draws_each_asset(profile, monkeypatch):
    fake = FakeStreamlit()
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    monkeypatch.setattr(status_view, "mqtt_reader",
                        lambda host, port, timeout=1.5: lambda topic: status(profile, "good"))
    runpy.run_path(str(ROOT / "services" / "dashboard" / "app.py"), run_name="__main__")
    page = "\n".join(fake.drawn)
    assert fake.run_every == 2.0
    assert f"**{profile['asset']['id']}** · NE 107 **good**" in page
    assert "#2e7d32" in page                     # the green marker
    assert "last window 2020-07-15T16:00:00Z" in page
