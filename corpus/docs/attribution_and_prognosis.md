# Reading the detector's attribution and the prognosis

PdM-Agent team note, 2026-10-10, CC BY 4.0.

## What the detector scores

The frozen detector is a principal component analysis (PCA) model of healthy operation over 21 features of each 10-minute window. Each feature is standardised with its healthy mean and standard deviation, the window is projected onto the healthy principal subspace (95 % of the variance), and the score is the sum of the squared reconstruction errors of the 21 features. A high score means the window's features do not fit together the way they do in healthy data. An alert is a score above the frozen threshold, which was set from the tail of healthy scores (SPOT).

## From alert to incident

One window over the threshold is not an incident. The persistence gate passes when at least 80 % of the windows ending in the last 30 minutes are over the threshold and at least 15 of them exist. A pass opens an incident unless one is still open from a pass less than 6 hours earlier.

## The attribution

Every score carries errors, each feature's squared error, and top, the five largest with their share of the score. A feature name is a signal name with the statistic over the window (mean, std, min, max, or slope, the last point minus the first), or an engineered channel: load_share_6h (share of the last 6 hours with the motor loaded), hours_since_rest (hours since motor current was last below 1 A), tp3_deficit_7d (the window's mean panel pressure minus the median of the past 7 days) and oil_rise_1h (oil temperature against an hour earlier). Common readings:

- Motor_current_min, hours_since_rest or load_share_6h on top: the motor never stopped, which is the continuous-load picture of a leak, and also of a restart refill.
- TP3_mean, TP3_min or tp3_deficit_7d on top with low values: panel pressure is low against normal or against the past week.
- Oil_temperature_mean on top: the oil is hotter than healthy, or colder after a start from cold.
- A large share on one pressure feature while its partner channels look normal: possibly an instrument; look at the plausibility check (C9).

## Limits of the attribution

The attribution names the features that do not fit, not the cause. Correlated features share the error, so the load features tend to rise together. A feature listed under imputed was missing, for example a 6-hour share with less than an hour of data after a gap, and took its healthy median, so its own error says nothing. In the 2020 data healthy weeks show the same pictures as failures: of the twelve false alarms on held-out healthy weeks, seven were runs without a stop, three were load commanded with the motor off and two were starts from cold. The checks, not the attribution, tell these cases apart.

## The prognosis

Every hour the prognostics service gives the probability of a failure within 24, 72 and 168 hours, each with a 90 % interval. The model is one logistic regression per horizon, made monotone across the horizons, and the interval runs from the 5th to the 95th percentile of 200 refits on bootstrapped days. Its inputs are the maximum and mean detector score and the share of windows over the threshold in the last 24 hours, and the 6-hour loaded and stopped shares and the hourly oil-temperature rise of the latest window. Its attribution lists each input's contribution to the log-odds of the 72-hour probability, largest first.

## How much to trust the prognosis

Each prognosis carries the model's measured skill. Scored leave-one-event-out against a constant hazard, its Brier skill is -0.03 at 24 hours, -0.08 at 72 hours and -0.13 at 168 hours: below the project's bar of 0.15 and below zero, so it does not yet beat the constant-hazard baseline. Read a probability only with its interval and beside the baseline that comes with it, and take a wide interval as a sign that the model is unsure. A brief never restates a probability; it cites the stored prognosis. The NE 107 status reads the bounds: the lower bound at 24 hours for failure, and the upper bound at 72 hours for maintenance required.

Sources in this repository: services/detector/README.md, services/prognostics/README.md, eval/frozen/metropt3_apu/detector/bundle.json, eval/frozen/metropt3_apu/survival/bundle.json, profiles/metropt3_apu/features.yaml, lib/pdm_common/gate.py, notebooks/detector_selection.ipynb Sections 2, 3 and 6.3.
