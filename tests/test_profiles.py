"""Config invariants: the facts the rest of the system depends on.

These are the checks that would have caught the 1 Hz / 15.2 M errors.
"""
from pathlib import Path

import yaml

PROFILE_DIR = Path(__file__).resolve().parents[1] / "profiles" / "metropt3_apu"


def _load(name):
    with open(PROFILE_DIR / name) as f:
        return yaml.safe_load(f)


def test_asset_profile_invariants():
    p = _load("asset_profile.yaml")
    assert p["asset"]["records"] == 1_516_948
    assert p["sampling"]["step_seconds"] == 10
    assert p["sampling"]["rate_hz"] == 0.1
    assert len(p["signals"]["analog"]) == 7
    assert len(p["signals"]["digital"]) == 8
    # every digital channel declares a polarity verdict
    assert all("polarity" in s for s in p["signals"]["digital"])
    # Reservoirs is excluded from detector inputs (r = 1.00 with TP3)
    assert "Reservoirs" not in p["detector"]["inputs"]
    # the feature pipeline (modality seam) is declared
    assert p["features"]["pipeline"]


def test_events_invariants():
    e = _load("events.yaml")
    assert len(e["failures"]) == 4
    ids = [f["id"] for f in e["failures"]]
    assert ids == ["F1", "F2", "F3", "F4"]
    # all four 2020 events are air leaks (verified against UCI, 2026-09-19)
    assert all(f["type"] == "air_leak" for f in e["failures"])
    # F2-F3 adjacency is declared, so the harness can pair the folds
    f3 = next(f for f in e["failures"] if f["id"] == "F3")
    assert f3.get("paired_with") == "F2"
    # the three padding types exist and are distinct settings
    assert set(e["guard_band"]) == {"before", "after"}
    assert set(e["healthy_buffer"]) == {"before_failure", "after_repair"}
    assert len(e["lead_horizons"]) == 3
    assert len(e.get("uncertain_periods", [])) >= 5


def test_no_asset_names_outside_profiles():
    """Portability guard: MetroPT signal names stay out of shared code."""
    repo = PROFILE_DIR.parents[1]
    offenders = []
    for py in (repo / "services").rglob("*.py"):
        text = py.read_text(errors="ignore")
        if any(name in text for name in ("TP2", "Oil_temperature", "Caudal_impulses")):
            offenders.append(str(py))
    assert not offenders, f"asset-specific names in shared code: {offenders}"
