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

## Result map

After monitoring completes, run the map dependency cell and map cell. The
optional `fimbox[map]` dependency supplies ipyleaflet/Jupyter widgets. A raster
selector lists retained `.cog`, `.tif`, and `.tiff` outputs, preferring COGs.
Pan/zoom to load visible 256-pixel tiles; at most 64 are requested per view.
Blue represents inundated cells; dry/nodata pixels are transparent.

The API must be redeployed with the raster endpoints and rio-tiler installed.
It uses the existing login and job ownership checks to read the manifest's
objects from `https://s3.hydroshare.org`. Private files need not be public.
COGs use HTTP range requests/overviews rather than full-file downloads; normal
TIFFs can be less efficient. Raster tiles travel through the API and notebook
kernel, not directly from S3 to the browser. S3 credentials remain on the API;
the FIMBox session remains in the kernel. No signed URLs are sent to the browser.
OpenStreetMap receives the normal basemap tile requests.

Rerun authentication/Connect and the map cell after session expiry. The Reload
tiles button retries reads and clears the in-memory tile cache. Expired job
ownership/results or deleted S3 objects cannot be recovered by rerunning the
map. Do not resubmit a job merely to refresh the map. Clear widget outputs
before saving/sharing: they contain raster imagery even though no credentials
are embedded. Static HTML exports cannot fetch new authenticated tiles.

The notebook has no saved credentials or outputs. Clear outputs from any
executed copy before sharing it. See the
[SDK and CLI guide](../sdk/README.md) for client setup and login details.
