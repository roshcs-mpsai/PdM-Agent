"""Asset status, read from the retained status topic (R0.5, HIL-01 stub).

The Streamlit page (app.py) and this module's command line both use the
functions below. The command line is the PRD's fallback for the R0 gate: if
the page is not working, demo the printed line instead.

    python services/dashboard/status_view.py                 # every profile, once
    python services/dashboard/status_view.py --watch 2       # refresh every 2 s

Topics come from each asset profile, never from code; with no --profile,
every profiles/*/asset_profile.yaml is shown.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from pdm_common.mqtt import read_retained
from pdm_common.profile import ProfileError, load_profile
from pdm_common.status import marker

ROOT = Path(__file__).resolve().parents[2]

Reader = Callable[[str], Optional[bytes]]


def discover_profiles(root: Path = ROOT) -> list[Path]:
    return sorted((root / "profiles").glob("*/asset_profile.yaml"))


def status_row(profile: dict, payload: Optional[bytes]) -> dict:
    """One asset's line on the board, from its retained status_v1 (or None)."""
    row = {"asset": profile["asset"]["id"], "topic": profile["topics"]["status"],
           "ts": None, "score": None, "threshold": None, "alert": None,
           "ne107": None, "detector_run": None, "marker": "grey", "note": "no status yet"}
    if payload is None:
        return row
    try:
        status = json.loads(payload)
        if status.get("schema") != "status_v1" or status.get("asset") != row["asset"]:
            raise ValueError("not a status_v1 for this asset")
    except ValueError as exc:
        return dict(row, marker="amber", note=f"unreadable status: {exc}")
    return dict(row, ts=status.get("ts"), score=status.get("score"),
                threshold=status.get("threshold"), alert=status.get("alert"),
                ne107=status.get("ne107"), detector_run=status.get("detector_run"),
                marker=marker(status.get("ne107")), note="")


def mqtt_reader(host: str, port: int, timeout: float = 1.5) -> Reader:
    def read(topic: str) -> Optional[bytes]:
        return read_retained(topic, host=host, port=port, timeout=timeout)
    return read


def board(profiles: list[dict], read: Reader) -> list[dict]:
    return [status_row(p, read(p["topics"]["status"])) for p in profiles]


def format_row(row: dict) -> str:
    if row["ne107"] is None:
        return f"{row['asset']:<16} [{row['marker']}] {row['note']} ({row['topic']})"
    return (f"{row['asset']:<16} [{row['marker']}] {row['ne107']:<21} last window {row['ts']}  "
            f"score {row['score']:.2f} / {row['threshold']:.2f}  run {row['detector_run']}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", action="append", help="asset profile (repeatable; default: all)")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--watch", type=float, default=0, metavar="SECONDS",
                   help="print again every SECONDS (default: once)")
    args = p.parse_args(argv)
    paths = [Path(x) for x in args.profile] if args.profile else discover_profiles()
    try:
        profiles = [load_profile(path) for path in paths]
    except ProfileError as exc:
        raise SystemExit(str(exc))
    read = mqtt_reader(args.broker, args.port)
    while True:
        try:
            rows = board(profiles, read)
        except OSError as exc:
            raise SystemExit(f"cannot reach the broker at {args.broker}:{args.port}: {exc}")
        for row in rows:
            print(format_row(row), flush=True)
        if not args.watch:
            return
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
