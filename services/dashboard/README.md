# Dashboard

Version Zero of the status board (R0.5, HIL-01 stub): one line per asset with
its NE 107 marker, the last window time (stream time), the score against the
threshold and the detector run ID, read from each asset's retained status
topic. Topics come from the asset profiles; nothing here names an asset.

```bash
pip install -r services/dashboard/requirements.txt   # streamlit, dashboard only
streamlit run services/dashboard/app.py               # refreshes every 2 s
```

`PDM_BROKER`, `PDM_BROKER_PORT` and `PDM_REFRESH_S` override localhost, 1883
and 2 s.

The same board prints to the terminal, which is the PRD's fallback for the R0
gate if the page is not working:

```bash
python services/dashboard/status_view.py             # once
python services/dashboard/status_view.py --watch 2   # every 2 s
```

```text
metropt3_apu     [green] good                  last window 2020-02-01T13:51:00Z  score 3.37 / 4.00  run zscore-45bc69dab7c9
```

Markers: green for `good`, amber for `out_of_specification`,
`maintenance_required` and `function_check`, red for `failure`, grey before
the first status arrives (`pdm_common.status`). In R0 the detector publishes
only `good` and `out_of_specification`.

R2b replaces this with the four-page dashboard (asset list, detail, review,
audit) reading through the API.
