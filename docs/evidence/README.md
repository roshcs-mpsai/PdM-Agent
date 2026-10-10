# Evidence

Pages generated from the evaluation's output, so the numbers in the report can be traced to a
run and regenerated. eval/out/ is not in git; these pages are.

| Page | Goal | Written by | From |
|---|---|---|---|
| `G1_G2.md` | G1 detection with warning at the false-alarm budget; G2 prognostic skill | `python eval/evidence.py` | the frozen evaluation in eval/out/freeze (`python eval/evaluate.py --frozen`, eval/prognostics.py) |
| `G3_scripted_incidents_rules.md` | G3 trajectory correctness and groundedness, the rules-policy baseline | `python eval/audit.py <run> --publish G3_scripted_incidents_rules.md` | `python eval/run_incidents.py --model rules` on the replayed campaign |

The pinned model's run of the scripted incidents (`eval/run_incidents.py --model anthropic`, with
`ANTHROPIC_API_KEY`) and its manual groundedness review (`claims.csv`, then `eval/audit.py --claims-review`)
are still to do; publish them next to the baseline as `G3_scripted_incidents_<model>.md`.

The scripted incidents run on a store of the whole campaign replayed through the deployed
service classes in one process (`python eval/pipeline.py --data "data/MetroPT3(AirCompressor).csv"
--store sqlite:///runs/campaign.db`, about 40 minutes on the build machine). On 2026-10-10 that
replay accepted all 1,516,948 records, produced the 239,172 complete windows of the evaluated
table, and the gate opened the 34 incidents of the batch gate at the same minutes.
