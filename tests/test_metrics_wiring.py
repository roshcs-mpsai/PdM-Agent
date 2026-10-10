"""The services count what OPS-02 asks for (T24): run ingest, detector, gate and
prognostics in process and read the counters back from the metrics registry."""
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for svc in ("replayer", "ingest", "detector", "gate", "prognostics"):
    sys.path.insert(0, str(ROOT / "services" / svc))

from pdm_common import metrics  # noqa: E402
from pdm_common.profile import load_profile, profile_hash  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"


def value(name, **labels):
    return metrics.REGISTRY.value(name, **labels) or 0.0


def test_stream_services_export_their_counts():
    import detector
    import gate
    import ingest
    import replayer
    from pdm_common.checks import load_registry
    from pdm_common.events import load_events
    from pdm_common.profile import events_file

    profile = load_profile(PROFILE_PATH)
    asset = profile["asset"]["id"]
    sha = profile_hash(PROFILE_PATH)
    i = ingest.Ingest(profile, sha, profile_path=PROFILE_PATH)
    d = detector.Detector(profile, detector.load_for_profile(profile, PROFILE_PATH), sha)
    g = gate.GateService(profile, sha, events=load_events(events_file(profile, PROFILE_PATH)),
                         registry=load_registry(profile, ROOT))
    names = {
        "in": ("pdm_records_in_total", {"asset": asset}),
        "accepted": ("pdm_records_accepted_total", {"asset": asset}),
        "quarantined": ("pdm_records_quarantined_total", {"asset": asset, "code": "unknown_asset"}),
        "published": ("pdm_windows_published_total", {"asset": asset}),
        "scored": ("pdm_windows_scored_total", {"asset": asset}),
        "scoring": ("pdm_scoring_seconds_count", {"asset": asset}),
        "opened": ("pdm_incidents_opened_total", {"asset": asset}),
        "decisions": ("pdm_gate_decisions_total", {"asset": asset, "decision": "open"}),
    }
    before = {k: value(n, **lab) for k, (n, lab) in names.items()}
    topics = profile["topics"]
    for seq in range(90):
        values = {}
        for s in profile["signals"]["analog"]:
            lo, hi = s["range"]
            values[s["name"]] = lo + (hi - lo) * (0.3 + 0.001 * seq)
        for s in profile["signals"]["digital"]:
            values[s["name"]] = float(seq % 2)
        _, payload = replayer.build_message(profile, seq, dt.datetime(2020, 6, 1) + dt.timedelta(seconds=10 * seq),
                                            values)
        for w in i.handle(payload):
            for sc in d.handle(w.payload):
                g.handle_score(sc.payload)
    i.handle(replayer.build_message(profile, 90, dt.datetime(2020, 6, 1, 0, 15), values)[1]
             .replace(f'"{asset}"', '"another_asset"'))
    for w in i.flush():
        if w.topic == topics["windows"]:
            for sc in d.handle(w.payload):
                g.handle_score(sc.payload)
    delta = {k: value(n, **lab) - before[k] for k, (n, lab) in names.items()}
    assert delta["in"] == 91 and delta["accepted"] == 90 and delta["quarantined"] == 1
    assert delta["published"] == delta["scored"] == delta["scoring"] == d.counts["scored"] > 0
    assert delta["opened"] == delta["decisions"] == g.counts["opened"]
    status = g.status[0]
    assert value("pdm_ne107_status", asset=asset, status=status) == 1
    text = metrics.REGISTRY.exposition()
    assert "pdm_scoring_seconds_bucket" in text and 'pdm_records_quarantined_total{asset="' in text
