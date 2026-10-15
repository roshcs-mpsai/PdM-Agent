"""Deterministic checks for the MetroPT-3 air production unit (T15b; AGT-05, PR-10).

One pure function per check, C1 to C11, each reading only the Evidence of one
alarm (pdm_common.checks): the 10 s grid before the incident opened, the
opening window's features and the gaps before it. Thresholds are the
profile's consistency_checks, recomputed from healthy operation by
tools/calibrate_checks.py; none was read off an event. Asset channel names
are allowed here, and only here (CFG-03).

Kinds:
* data_quality (C1-C3): the alarm may have a benign cause in the data;
* fault_signature (C4-C8, C11): the data shows a developing fault;
* instrument (C9, C10): a sensor, not the compressor, may be at fault.

Ported from notebooks/detector_selection_v7.2.ipynb, Sections 8.2 and 8.7;
tests/test_checks.py holds the port to the notebook's 41 evidence packets.
"""
from __future__ import annotations

import numpy as np

from pdm_common.checks import CheckResult, Evidence, ago

from . import triage  # noqa: F401  (the triage rules, evidence only)
from .triage import triage_evidence  # noqa: F401

LOADED_A = 4.5          # motor loaded (EDA 8.1)
STOPPED_A = 1.0         # motor stopped


def motor_off_command(ev: Evidence) -> CheckResult:
    """C1: load commanded (DV_eletric = 1) while the motor current is under 1 A in the opening window."""
    n = ev.feature("cmd_motor_off")
    n = 0.0 if np.isnan(n) else n
    points = int(ev.window_length / ev.step)
    return ev.result("C1", "fail" if n > 0 else "pass",
                     f"{int(n)} of {points} points in the opening window had load commanded with motor current "
                     f"under {STOPPED_A:g} A", value=n)


def restart(ev: Evidence) -> CheckResult:
    """C2: data resumed after a gap less than the evidence span plus the gap edge before the alarm."""
    gap = ev.last_gap()
    if gap is None:
        return ev.result("C2", "pass", f"no gap of {ev.gap_min.total_seconds() / 60:.0f} min or more before the alarm")
    since = (ev.opens - gap.after).total_seconds() / 60
    within = ev.restart_within.total_seconds() / 60
    return ev.result("C2", "fail" if since < within else "pass",
                     f"data resumed {ago(since)} before the alarm, after {gap.hours:.1f} h without data",
                     value=since, threshold=within)


def bridged_points(ev: Evidence) -> CheckResult:
    """C3: any interpolated grid point in the data behind the gate."""
    rows = ev.behind()
    n = int(ev.grid["bridged"].to_numpy()[rows].sum())
    return ev.result("C3", "fail" if n > 0 else "pass",
                     f"{n} interpolated points in the {ev.behind_span.total_seconds() / 60:.0f} min behind the alarm",
                     value=n)


def continuous_load_signature(ev: Evidence) -> CheckResult:
    """C4: the motor has run without a rest for longer than healthy operation ever does."""
    h, limit = ev.feature("hours_since_rest"), ev.threshold("C4")
    if np.isnan(h):
        return ev.result("C4", "inconclusive", "no rest of the motor in the data available", value=h)
    return ev.result("C4", "fail" if h > limit else "pass",
                     f"motor has run without a rest for {h:.2f} h (healthy 99th percentile {limit:.2f} h)", value=h)


def load_share_rise(ev: Evidence) -> CheckResult:
    """C5: the motor was loaded for more of the past 6 h than healthy operation ever is."""
    share, limit = ev.feature("load_share_6h"), ev.threshold("C5")
    if np.isnan(share):
        return ev.result("C5", "inconclusive", "under 1 h of data in the past 6 h", value=share)
    return ev.result("C5", "fail" if share > limit else "pass",
                     f"motor loaded for {share:.0%} of the past 6 h (healthy 99th percentile {limit:.0%})",
                     value=share)


def oil_temperature_high(ev: Evidence) -> CheckResult:
    """C6: oil hotter in the opening window than healthy operation ever is."""
    oil, limit = ev.feature("Oil_temperature_mean"), ev.threshold("C6")
    if np.isnan(oil):
        return ev.result("C6", "inconclusive", "no oil temperature in the opening window", value=oil)
    return ev.result("C6", "fail" if oil > limit else "pass",
                     f"oil at {oil:.1f} degC in the opening window (healthy 99th percentile {limit:.1f})", value=oil)


def lps_activity(ev: Evidence) -> CheckResult:
    """C7: the low-pressure switch was active for longer in the past 24 h than healthy operation allows."""
    minutes, limit = ev.feature("lps_minutes_24h"), ev.threshold("C7")
    minutes = 0.0 if np.isnan(minutes) else minutes
    return ev.result("C7", "fail" if minutes > limit else "pass",
                     f"low-pressure switch active for {minutes:.0f} min in the past 24 h "
                     f"(healthy 99th percentile {limit:.0f} min)", value=minutes)


def leak_rate_across(ev: Evidence, gap) -> float:
    """Panel pressure lost per hour across a gap (bar/h)."""
    tp3 = ev.channel("TP3")
    return float((tp3[gap.before_pos] - tp3[gap.after_pos]) / gap.hours)


def leak_down(ev: Evidence) -> CheckResult:
    """C8: panel pressure fell faster across a recent gap than across healthy gaps."""
    gap, limit = ev.last_gap(), ev.threshold("C8")
    lookback = ev.hold_off
    if gap is None or ev.opens - gap.after > lookback:
        return ev.result("C8", "inconclusive",
                         f"no gap of {ev.gap_min.total_seconds() / 60:.0f} min or more ended in the "
                         f"{lookback.total_seconds() / 3600:.0f} h before the alarm")
    rate = leak_rate_across(ev, gap)
    return ev.result("C8", "fail" if rate > limit else "pass",
                     f"panel pressure fell {rate:.2f} bar/h across the last gap (healthy 99th percentile {limit:.2f})",
                     value=rate)


def tp3_reservoirs_divergence(ev: Evidence) -> CheckResult:
    """C9: panel and reservoir pressure apart by more than the threshold for the sustain time, behind the gate.
    The two track each other at r = 1.00 in healthy data, so a sustained gap points to one sensor."""
    limit = ev.threshold("C9")
    sustain = int(_minutes(ev.param("C9", "sustain", "10min")) * ev.points_per_minute)
    rows = ev.behind()
    if rows.stop <= rows.start:
        return ev.result("C9", "inconclusive", "no data behind the gate")
    tp3, res = ev.channel("TP3"), ev.grid["Reservoirs"].to_numpy(float)
    with np.errstate(invalid="ignore"):
        apart = ev.valid & (np.abs(tp3 - res) > limit)
    run = np.convolve(apart.astype(int), np.ones(sustain, int), mode="full")[:len(apart)] == sustain
    fails = bool(run[rows].any())
    widest = np.nanmax(np.abs(tp3[rows] - res[rows])) if np.isfinite(tp3[rows]).any() else np.nan
    return ev.result("C9", "fail" if fails else "pass",
                     "panel and reservoir pressure " + (f"more than {limit:g} bar apart for "
                                                        f"{sustain // ev.points_per_minute} min" if fails
                                                        else f"within {limit:g} bar of each other"),
                     value=widest)


def digital_polarity_audit(ev: Evidence) -> CheckResult:
    """C10: inconclusive until the three suspect digital channels are settled (profile signals.digital)."""
    suspect = [s["name"] for s in ev.profile["signals"]["digital"] if s.get("polarity") == "suspect_inverted"]
    return ev.result("C10", "inconclusive",
                     f"{len(suspect)} digital channels not yet verified ({', '.join(suspect)}); none is used by the checks")


def refill_rate(ev: Evidence) -> CheckResult:
    """C11: after a restart, panel pressure refilled more slowly than the slowest tenth of healthy restarts.
    Applies only to an alarm that opens within the restart span (C2 fails); otherwise inconclusive."""
    gap, limit = ev.last_gap(), ev.threshold("C11")
    if gap is None or ev.opens - gap.after >= ev.restart_within:
        return ev.result("C11", "inconclusive", "not a restart alarm")
    rate = restart_refill_rate(ev, gap.after_pos)
    if np.isnan(rate):
        return ev.result("C11", "inconclusive",
                         f"under {ev.param('C11', 'min_minutes', 5)} min with the motor loaded below "
                         f"{ev.param('C11', 'cut_out_bar', 9.5)} bar since data resumed")
    return ev.result("C11", "fail" if rate < limit else "pass",
                     f"panel pressure refilled at {rate:.3f} bar/min after data resumed "
                     f"(healthy restarts' 10th percentile {limit:.3f})", value=rate)


def restart_refill_rate(ev: Evidence, resume_pos: int, until=None) -> float:
    """Panel pressure gained per minute while the motor is loaded below cut-out, from data resumption to
    ``until`` (default the opening) and within the restart horizon; NaN with too little such data."""
    horizon = ev.window_length + (ev.min_present - 1) * ev.stride          # 24 min: the earliest opening
    until = ev.opens if until is None else until
    start = ev.t[resume_pos]
    end = min(until, start + horizon)
    seg = slice(resume_pos, int(ev.t.searchsorted(end)))
    tp3, cur = ev.channel("TP3")[seg], ev.channel("Motor_current")[seg]
    with np.errstate(invalid="ignore"):
        m = (cur > float(ev.param("C11", "loaded_above_a", LOADED_A))) & (tp3 < float(ev.param("C11", "cut_out_bar", 9.5)))
    need = int(float(ev.param("C11", "min_minutes", 5)) * ev.points_per_minute)
    if m.sum() < need:
        return float("nan")
    minutes = (ev.t[seg][m] - ev.t[seg][m][0]).total_seconds().to_numpy() / 60
    return float(np.polyfit(minutes, tp3[m], 1)[0])


def _minutes(text) -> float:
    from pdm_common.timeutil import parse_duration
    return parse_duration(text).total_seconds() / 60


REGISTRY = {
    "C1": motor_off_command,
    "C2": restart,
    "C3": bridged_points,
    "C4": continuous_load_signature,
    "C5": load_share_rise,
    "C6": oil_temperature_high,
    "C7": lps_activity,
    "C8": leak_down,
    "C9": tp3_reservoirs_divergence,
    "C10": digital_polarity_audit,
    "C11": refill_rate,
}
