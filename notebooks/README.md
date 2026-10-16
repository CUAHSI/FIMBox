# Cloud job notebook

[`cloud_hand_fim.ipynb`](cloud_hand_fim.ipynb) demonstrates submitting and
monitoring a job against a cloud FIMBox API using the generated Python SDK.
It accepts an API URL and HydroShare output bucket, provides a browser login
link, and polls job status and output metadata.

Open the notebook in Jupyter or VS Code with a Python kernel. Click the login
link while the authentication cell waits, then run the separate Connect cell.
Set `RESUME_JOB_ID` to monitor an existing job after restarting the kernel;
otherwise, submission starts a real cloud computation that may incur cost.

The notebook has no saved credentials or outputs. Clear outputs from any
executed copy before sharing it. See the
[SDK and CLI guide](../sdk/README.md) for client setup and login details.
