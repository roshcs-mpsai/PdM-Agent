"""Leave-one-event-out evaluation harness -- Version Zero.

Loads the event configuration and prints the fold plan a real run will execute.
Metrics are deliberately 'not implemented': this file exists so the harness has
a shape before Week 5, per the construction plan. It must keep running from the
repo root with no arguments.
"""
from pathlib import Path

import yaml

EVENTS = Path(__file__).resolve().parents[1] / "profiles" / "metropt3_apu" / "events.yaml"


def load_events(path: Path = EVENTS) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main() -> None:
    cfg = load_events()
    fails = cfg["failures"]
    uncertain = cfg.get("uncertain_periods", [])

    print(f"asset            : {cfg['asset']}")
    print(f"campaign         : {cfg['campaign'][0]} -> {cfg['campaign'][1]}")
    print(f"guard band       : {cfg['guard_band']['before']} before / "
          f"{cfg['guard_band']['after']} after")
    print(f"healthy buffer   : {cfg['healthy_buffer']['before_failure']} before / "
          f"{cfg['healthy_buffer']['after_repair']} after repair")
    print(f"lead horizons    : {', '.join(str(h) for h in cfg['lead_horizons'])}")
    print(f"uncertain periods: {len(uncertain)} (excluded from healthy; "
          f"FAR reported with and without)")
    print()

    for f in fails:
        paired = f" [paired with {f['paired_with']}]" if f.get("paired_with") else ""
        print(f"fold holds out {f['id']} ({f['type']}, {f['start']} -> {f['end']}){paired}")
        print("  lead time        : not implemented")
        print("  false alarms/week: not implemented (dual reporting)")
        print("  P(24/72/168 h)   : not implemented")
    print("\npooled figures are reported only alongside n=4.")


if __name__ == "__main__":
    main()
