# Google Cloud deployment

The Terraform configuration deploys separate API and HAND-FIM worker images,
Cloud Run services, a worker pool, Memorystore Redis, networking, Artifact
Registry, IAM, and Secret Manager resources. Cloud deployment **requires**
HydroShare authentication and an S3 output bucket; it does not use the shared
anonymous local mode or a shared output mount.

Provisioning creates billable resources. Use a billed GCP project and verify
Cloud Run worker-pool availability and quota in the selected region.

## 1. Bootstrap infrastructure

Requirements: Google Cloud CLI, Terraform 1.5+, Docker, and permission to
enable services and manage the resources.

```bash
gcloud auth application-default login
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Set `project_id` and any desired region/name values in `terraform.tfvars`.
Keep `deploy_services = false` for the first apply:

```bash
terraform -chdir=terraform init
terraform -chdir=terraform plan
terraform -chdir=terraform apply
```

Terraform does not build images. It creates OAuth secret containers empty
unless both `hydroshare_client_id` and `hydroshare_client_secret` are supplied.
Supplying credentials through tfvars stores their values in Terraform state
and saved plans. Alternatively, populate Secret Manager externally and leave
both inputs null. Protect state and plans; use secured remote state for
production. Never commit credentials, tfvars, state, or plans.

## 2. Build and push images

Use a unique tag for each release:

```bash
bash terraform/build_push_images.sh release-tag
```

The script builds and pushes both Linux/amd64 images and prints the tfvars
assignments after both pushes succeed. Copy the assignments into `terraform.tfvars`;
then set `deploy_services = true`. It does not edit the file or apply Terraform.

## 3. Set credentials and deploy

Register a confidential HydroShare OAuth client. Either supply both credentials
through tfvars, or populate the provisioned Secret Manager secrets externally.
The API reads the OAuth secrets; workers receive temporary per-job S3
credentials through Redis. Configure `s3_endpoint_url` to an endpoint that
accepts HydroShare-issued credentials.

Set the image references and enable services:

```hcl
deploy_services = true
api_image       = "REGISTRY_FROM_OUTPUT/api:release-tag"
worker_image    = "REGISTRY_FROM_OUTPUT/worker:release-tag"
```

Apply, then set `public_api_url` to the generated `api_url` (or your configured
HTTPS origin), without a trailing slash, and apply again. Register both exact
URLs with the HydroShare OAuth client:

- `<public_api_url>/auth/callback`
- `<public_api_url>/docs/oauth2-redirect`

The placeholder origin disables usable redirects until replaced. Verify
authentication and submit a job:

```bash
export FIMBOX_API_URL="$(terraform -chdir=terraform output -raw api_url)"
curl --fail "$FIMBOX_API_URL/auth/config"
pixi run fimbox submit --body test-data/single_reach.json \
  --output-bucket YOUR_HYDROSHARE_BUCKET --output-prefix fimbox
```

The output bucket/resource must already exist and be writable by the
submitting user. Artifacts are uploaded to S3; the API returns metadata and
does not provide artifact downloads.

## Operations and validation

The worker pool is always running (defaults: one worker, 4 CPUs, 16 GiB).
Scratch files and HAND-FIM caches consume its memory budget. One worker is
supported per shared watershed cache; this configuration does not provision
Filestore or a queue autoscaler. Job metadata expires according to
`result_ttl_seconds`; Redis is not a durable job archive. Do not scale down or
destroy the deployment during active work.

Streaming mode avoids the whole-HUC scratch budget. Publish a prepared dataset
(see [the HAND-FIM guide](../docker/handfim/README.md#streaming-mode)) and set
`stream_input_bucket`/`stream_input_prefix` (plus `stream_input_unsigned` for a
public bucket). Set `stream_worker_instance_count` to add a dedicated pool
(`stream_worker_memory`, default 4Gi; `stream_worker_cpu`, default 2) that
consumes a separate `streaming` queue; otherwise the default worker runs
streaming jobs too.

Validate without applying:

```bash
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
terraform -chdir=terraform test
bash tests/test_build_push_images.sh
```

Terraform mock-provider tests require 1.7+. Inspect `terraform plan -destroy`
before removing resources; Terraform does not remove artifacts already stored
in HydroShare. For module variables, inspect
[`terraform.tfvars.example`](terraform.tfvars.example).
