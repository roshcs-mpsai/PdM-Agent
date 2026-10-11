"""NE 107 status (INC-07, INT-03) and the dashboard marker colours.

``ne107`` derives an asset's status from what the gate service knows: the
latest score, the gate episode, the incident and its diagnosis, the latest
prognosis, drift alerts, out-of-range readings and how long ago data last
arrived. When several conditions hold, the most severe wins: failure, then
function check, out of specification, maintenance required, good (FRD
section 8, with the profile's ``ne107`` thresholds):

* failure -- an incident is open and the continuous-load signature check
  fails, or the lower bound of P(24 h) exceeds ``failure_p24_lower``;
* function check -- no data for longer than ``no_data_after``, or a model
  promotion in progress;
* out of specification -- an alert while the gate has not passed, a drift
  alert, an out-of-range reading, or a diagnosis that blames an instrument;
* maintenance required -- an incident open with a process diagnosis or none
  yet, or the upper bound of P(72 h) exceeds ``maintenance_p72_upper``;
* good -- none of these.

Two readings of the FRD are ours: the continuous-load signature counts only
inside an open incident (alone it fires in 1 % of healthy windows by
construction of its threshold), and an incident still waiting for its
diagnosis shows maintenance required rather than good.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Most severe first: when several conditions hold, the first one wins.
NE107_STATES = (
    "failure",
    "function_check",
    "out_of_specification",
    "maintenance_required",
    "good",
)

MARKER = {
    "good": "green",
    "maintenance_required": "amber",
    "out_of_specification": "amber",
    "function_check": "amber",
    "failure": "red",
}


@dataclass
class StatusInputs:
    alert: bool = False                  # the latest score is over the threshold
    episode_open: bool = False           # the gate has passed and the episode is open
    incident_open: bool = False          # an incident not yet closed
    diagnosis: str | None = None         # "process", "instrumentation", "insufficient" or None (not yet)
    continuous_load: bool = False        # the continuous-load signature check fails on the latest window
    p24_lower: float | None = None
    p72_upper: float | None = None
    drift: bool = False
    out_of_range: list = field(default_factory=list)    # signals flagged in the latest window
    no_data: bool = False
    promotion: bool = False


def ne107(x: StatusInputs, params: dict) -> tuple[str, list[str]]:
    """(state, causes): the most severe state that holds and every cause behind it."""
    found = {s: [] for s in NE107_STATES}
    if x.incident_open and x.continuous_load:
        found["failure"].append("continuous-load signature during an open incident")
    if x.p24_lower is not None and x.p24_lower > float(params.get("failure_p24_lower", 0.5)):
        found["failure"].append(f"P(24 h) lower bound {x.p24_lower:.2f} over {params.get('failure_p24_lower')}")
    if x.no_data:
        found["function_check"].append(f"no data for longer than {params.get('no_data_after')}")
    if x.promotion:
        found["function_check"].append("model promotion in progress")
    if x.alert and not x.episode_open:
        found["out_of_specification"].append("alert active, gate not yet passed")
    if x.drift:
        found["out_of_specification"].append("drift alert")
    if x.out_of_range:
        found["out_of_specification"].append("out-of-range reading on " + ", ".join(sorted(x.out_of_range)))
    if x.incident_open and x.diagnosis == "instrumentation":
        found["out_of_specification"].append("diagnosis points to an instrument")
    if x.incident_open and x.diagnosis in (None, "process", "insufficient"):
        found["maintenance_required"].append("incident open, " + {None: "diagnosis pending",
                                                                 "process": "process diagnosis",
                                                                 "insufficient": "evidence insufficient"}[x.diagnosis])
    if x.p72_upper is not None and x.p72_upper > float(params.get("maintenance_p72_upper", 0.5)):
        found["maintenance_required"].append(f"P(72 h) upper bound {x.p72_upper:.2f} over "
                                             f"{params.get('maintenance_p72_upper')}")
    for state in NE107_STATES[:-1]:
        if found[state]:
            return state, found[state]
    return "good", []


def marker(state: str) -> str:
    """Dashboard colour for an NE 107 state; unknown states show amber."""
    return MARKER.get(state, "amber")
