# NE 107 status: what the states mean for a planner

PdM-Agent team note, 2026-10-10, CC BY 4.0.

## Where the states come from

NAMUR recommendation NE 107 defines a small set of status signals for field devices, so that operators and maintenance staff see the same few categories whatever the device: failure, function check, out of specification and maintenance required, with good when none applies. PdM-Agent uses the same states for the whole air production unit rather than for a single device.

## How PdM-Agent sets the status

The gate service derives the status from the latest score, the persistence gate, the open incident and its diagnosis, the latest prognosis, drift alerts, out-of-range readings and how long ago data last arrived. When several conditions hold, the most severe wins, in this order:

- Failure: an incident is open and the continuous-load check (C4) fails on the latest window, or the lower bound of the probability of failure within 24 hours is above 0.5.
- Function check: no data for more than 5 minutes, or a model promotion in progress.
- Out of specification: the score is over the threshold but the persistence gate has not passed yet; a drift alert; a reading outside its plausibility range; or a diagnosis that blames an instrument.
- Maintenance required: an incident is open with a process diagnosis, with its diagnosis still pending, or with insufficient evidence; or the upper bound of the probability of failure within 72 hours is above 0.5.
- Good: none of these.

The dashboard shows failure in red, the three middle states in amber and good in green, and every change of status is logged with its causes. The probability bounds and the no-data time are proposed values in the asset profile and may change; the causes listed with each status say which condition applied.

## What the states mean in practice

- A failure status means the evidence says the unit is failing now. Read the brief and act under the operator's rules for a failing air supply; the work order in the brief is still a draft until someone approves it.
- A function check status means the monitoring, not necessarily the unit, has a problem: data have stopped or a model is being changed. Fix the data path before drawing conclusions about the asset, and do not send anyone out on the model's word alone.
- An out of specification status means something is outside its normal range but the evidence for a fault is not there. A short alert often clears itself; a drift alert or an instrument diagnosis calls for a sensor check rather than a repair.
- A maintenance required status means an incident is open or the medium-term risk is raised. Plan an inspection within the planning horizon, using the brief's hypotheses and check results to decide what to look at.
- A good status means there is nothing to do.

## Two choices in PdM-Agent's mapping

The continuous-load check counts towards failure only inside an open incident: on its own it fires in 1 % of healthy windows, by the way its threshold was set. And an incident still waiting for its diagnosis shows maintenance required rather than good, so an alarm is never hidden while the agent works on it.

Sources in this repository: lib/pdm_common/status.py, profiles/metropt3_apu/asset_profile.yaml (ne107), services/gate/README.md.
