"""NE 107 status vocabulary (INC-07, INT-03) and the dashboard marker colours.

R0 derives status from the detector's alert flag alone: an active alert is
``out_of_specification`` (FRD section 8: alert active, gate not yet passed),
otherwise ``good``. The gate service takes this over in R2b with the full
FRD mapping.
"""
from __future__ import annotations

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


def r0_status(alert: bool) -> str:
    """Walking-skeleton status: the alert flag only, no gate yet."""
    return "out_of_specification" if alert else "good"


def marker(state: str) -> str:
    """Dashboard colour for an NE 107 state; unknown states show amber."""
    return MARKER.get(state, "amber")
