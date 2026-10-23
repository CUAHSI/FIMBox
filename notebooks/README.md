# Cloud job notebook

[`cloud_hand_fim.ipynb`](cloud_hand_fim.ipynb) demonstrates submitting and
monitoring a job against a cloud FIMBox API using the generated Python SDK.
It accepts an API URL and HydroShare output bucket, provides a browser login
link, and polls job status and output metadata.

Open the notebook in Jupyter or VS Code with a Python kernel. Click the login
link while the authentication cell waits, then run the separate Connect cell.
Set `RESUME_JOB_ID` to monitor an existing job after restarting the kernel;
otherwise, submission starts a real cloud computation that may incur cost.
Before submission, choose the inline scenario example or the JSON-loading cell,
which reads `test-data/single_reach.json` into generated SDK `Scenario` models.
The submission cell uses the current `scenarios` value without replacing it.
Both execution modes retain the input scenario array at
`s3://<OUTPUT_BUCKET>/<OUTPUT_PREFIX>/scenarios.json` alongside the output data.
This JSON uses the original scenario IDs, reaches, and streamflows and can be
reused as scenario input. It is listed in the result's `files` metadata and
follows the same overwrite setting as the other output objects.
The accompanying `README.md` explains the output layout and input units, records
the execution settings, and provides rerun and path-based viewing examples.

## Result map

Install the optional `fimbox[map]` dependency for ipyleaflet/Jupyter widgets,
then run the map cell. `fimbox_map.output_map(api_client, SCENARIO_PATHS)` loads
retained outputs directly from `s3://bucket/key` paths, not an expiring job ID.
The example uses `EXECUTION_MODE` to build legacy `<ScenarioID>.cog` paths or
streaming `<ScenarioID>/manifest.json` paths from the configured destination
and scenario names. Match the mode and `OUTPUT_PREFIX` to the saved run being
viewed; legacy runs do not produce streaming manifests.
Edit `SCENARIO_PATHS` to use explicit manifest
paths or `.cog`, `.tif`, and `.tiff` object paths (prefer COGs).
To reopen outputs after 24 hours or a kernel restart, run setup, configuration,
authentication/Connect, scenario inputs, and the map cell. Do not submit or
monitor a job just to reopen a map. Neither `job_id` nor `final_result` is needed.
Pan/zoom to load visible 256-pixel tiles; at most 64 are requested per view.
Blue represents inundated cells; dry/nodata pixels are transparent.

The API must be redeployed with the raster endpoints and rio-tiler installed.
The path-based `/outputs` endpoints use the existing login and current user's
HydroShare S3 permissions to read objects from `https://s3.hydroshare.org`.
Job ownership/result expiry does not affect these reads. Private files need not be public.
COGs use HTTP range requests/overviews rather than full-file downloads; normal
TIFFs can be less efficient. Raster tiles travel through the API and notebook
kernel, not directly from S3 to the browser. S3 credentials remain on the API;
the FIMBox session remains in the kernel. No signed URLs are sent to the browser.
OpenStreetMap receives the normal basemap tile requests.

Rerun authentication/Connect and the map cell after session expiry. The Reload
tiles button retries reads and clears the in-memory tile cache. Deleted S3
objects or revoked HydroShare permissions still prevent viewing.
Do not resubmit a job merely to refresh the map. Clear widget outputs
before saving/sharing: they contain raster imagery even though no credentials
are embedded. Static HTML exports cannot fetch new authenticated tiles.

The notebook has no saved credentials or outputs. Clear outputs from any
executed copy before sharing it. See the
[SDK and CLI guide](../sdk/README.md) for client setup and login details.
