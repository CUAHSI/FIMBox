# Sample scenarios

Use these JSON files with `fimbox submit --body PATH`:

- [`single_reach.json`](single_reach.json) — one reach in one HUC.
- [`single_huc_many_reaches.json`](single_huc_many_reaches.json) — multiple
  reaches in one HUC, merged into one scenario output.
- [`many_hucs_many_reaches.json`](many_hucs_many_reaches.json) — reaches from
  multiple HUCs, merged into one scenario output.
- [`multiple_scenarios.json`](multiple_scenarios.json) — independent scenarios
  submitted as one job.

Each scenario needs a unique `ScenarioID` and a list of reaches containing an
eight-digit `HUC`, `ReachID`, and nonnegative `Streamflow` in cubic metres per
second. Use a real reach with available HAND data. The sample HUC/reach pair
`03020201` / `8780625` is used in the single-reach example.

Outputs are grouped under `scenarios/<job_id>/`. Local files are temporary;
provide a HydroShare S3 bucket to retain them. See the
[API guide](../docker/api/README.md) for job and output details.
