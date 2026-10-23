mock_provider "google" {}
mock_provider "google-beta" {}

variables {
  project_id               = "fimbox-test"
  hydroshare_client_id     = null
  hydroshare_client_secret = null
  deploy_services          = false
  api_image                = ""
  worker_image             = ""
}

run "bootstrap_without_images_or_credentials" {
  command = plan
  assert {
    condition     = length(google_cloud_run_v2_service.api) == 0 && length(google_cloud_run_v2_worker_pool.worker) == 0
    error_message = "Bootstrap must not deploy an anonymous or unconfigured runtime."
  }
  assert {
    condition     = length(google_secret_manager_secret.oauth) == 2
    error_message = "Bootstrap must provision both OAuth secret containers."
  }
  assert {
    condition     = length(google_secret_manager_secret_version.oauth) == 0
    error_message = "Null credential inputs must preserve external secret management."
  }
}

run "authenticated_s3_deployment" {
  command = plan
  variables {
    deploy_services     = true
    api_image           = "us-central1-docker.pkg.dev/fimbox-test/fimbox-dev/api:v1"
    worker_image        = "us-central1-docker.pkg.dev/fimbox-test/fimbox-dev/worker:v1"
    public_api_url      = "https://fimbox.example.org"
    api_min_instances   = 1
    api_concurrency     = 12
    api_timeout_seconds = 120
  }
  assert {
    condition = (
      google_cloud_run_v2_service.api[0].template[0].scaling[0].min_instance_count == 1 &&
      google_cloud_run_v2_service.api[0].template[0].max_instance_request_concurrency == 12 &&
      google_cloud_run_v2_service.api[0].template[0].timeout == "120s"
    )
    error_message = "API tuning variables must configure Cloud Run rather than remain hardcoded."
  }
  assert {
    condition = one([
      for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env :
      env.value if env.name == "REQUIRE_HYDROSHARE_OUTPUT"
    ]) == "true"
    error_message = "The public API must require authentication and HydroShare S3 output."
  }
  assert {
    condition = one([
      for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env :
      env.value if env.name == "HYDROSHARE_OAUTH_REDIRECT_URI"
    ]) == "https://fimbox.example.org/auth/callback"
    error_message = "OAuth must redirect to the deployed HTTPS API."
  }
  assert {
    condition = length([
      for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env :
      env if contains(["HYDROSHARE_CLIENT_ID", "HYDROSHARE_CLIENT_SECRET"], env.name) &&
      length(env.value_source) == 1
    ]) == 2
    error_message = "Both OAuth credentials must come from Secret Manager."
  }
  assert {
    condition = length([
      for env in google_cloud_run_v2_worker_pool.worker[0].template[0].containers[0].env :
      env if contains(["HYDROSHARE_CLIENT_ID", "HYDROSHARE_CLIENT_SECRET"], env.name)
    ]) == 0
    error_message = "The worker must not receive OAuth application secrets."
  }
  assert {
    condition = google_cloud_run_v2_service.api[0].template[0].containers[0].image != (
      google_cloud_run_v2_worker_pool.worker[0].template[0].containers[0].image
    )
    error_message = "The slim API and scientific worker must use separate images."
  }
}

run "services_require_images" {
  command = plan
  variables {
    deploy_services = true
  }
  expect_failures = [
    google_cloud_run_v2_service.api[0],
    google_cloud_run_v2_worker_pool.worker[0],
  ]
}

run "tfvars_credentials_and_custom_s3" {
  command = apply
  variables {
    deploy_services          = true
    api_image                = "us-central1-docker.pkg.dev/fimbox-test/fimbox-dev/api:v1"
    worker_image             = "us-central1-docker.pkg.dev/fimbox-test/fimbox-dev/worker:v1"
    hydroshare_client_id     = "test-client"
    hydroshare_client_secret = "test-only-secret"
    s3_endpoint_url          = "https://objects.example.org"
  }
  assert {
    condition     = length(google_secret_manager_secret_version.oauth) == 2
    error_message = "Both credential inputs must create Secret Manager versions."
  }
  assert {
    condition = (
      google_secret_manager_secret_version.oauth["client-id"].secret_data == var.hydroshare_client_id &&
      google_secret_manager_secret_version.oauth["client-secret"].secret_data == var.hydroshare_client_secret
    )
    error_message = "Secret Manager must receive the supplied credential values."
  }
  assert {
    condition = alltrue([
      for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env :
      env.value_source[0].secret_key_ref[0].version == google_secret_manager_secret_version.oauth[
        env.name == "HYDROSHARE_CLIENT_ID" ? "client-id" : "client-secret"
      ].version if contains(["HYDROSHARE_CLIENT_ID", "HYDROSHARE_CLIENT_SECRET"], env.name)
    ])
    error_message = "The API must use the Terraform-managed OAuth versions."
  }
  assert {
    condition = one([
      for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env :
      env.value if env.name == "S3_ENDPOINT_URL"
      ]) == var.s3_endpoint_url && one([
      for env in google_cloud_run_v2_worker_pool.worker[0].template[0].containers[0].env :
      env.value if env.name == "S3_ENDPOINT_URL"
    ]) == var.s3_endpoint_url
    error_message = "API signing and worker uploads must use the same configured S3 endpoint."
  }
}

run "streaming_worker_pool" {
  command = plan
  variables {
    deploy_services              = true
    api_image                    = "us-central1-docker.pkg.dev/fimbox-test/fimbox-dev/api:v1"
    worker_image                 = "us-central1-docker.pkg.dev/fimbox-test/fimbox-dev/worker:v1"
    stream_input_bucket          = "hydrofabric"
    stream_input_prefix          = "hand/v1"
    stream_worker_instance_count = 1
  }
  assert {
    condition     = google_cloud_run_v2_worker_pool.stream_worker[0].template[0].containers[0].resources[0].limits.memory == "4Gi"
    error_message = "Streaming workers must default to the 4 GiB budget."
  }
  assert {
    condition = one([
      for env in google_cloud_run_v2_worker_pool.stream_worker[0].template[0].containers[0].env :
      env.value if env.name == "CELERY_TASK_DEFAULT_QUEUE"
      ]) == "streaming" && one([
      for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env :
      env.value if env.name == "STREAM_QUEUE"
    ]) == "streaming"
    error_message = "The API must route streaming jobs to the queue the streaming pool consumes."
  }
  assert {
    condition = one([
      for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env :
      env.value if env.name == "STREAM_INPUT_BUCKET"
    ]) == "hydrofabric"
    error_message = "The API must know the dataset bucket to accept streaming jobs."
  }
}

run "streaming_workers_require_dataset" {
  command = plan
  variables {
    deploy_services              = true
    api_image                    = "us-central1-docker.pkg.dev/fimbox-test/fimbox-dev/api:v1"
    worker_image                 = "us-central1-docker.pkg.dev/fimbox-test/fimbox-dev/worker:v1"
    stream_worker_instance_count = 1
  }
  expect_failures = [google_cloud_run_v2_worker_pool.stream_worker[0]]
}

run "partial_credentials_rejected" {
  command = plan
  variables {
    hydroshare_client_id     = "test-client"
    hydroshare_client_secret = null
  }
  expect_failures = [
    google_secret_manager_secret.oauth["client-id"],
    google_secret_manager_secret.oauth["client-secret"],
  ]
}

run "invalid_endpoint_rejected" {
  command = plan
  variables {
    s3_endpoint_url = "http://objects.example.org"
  }
  expect_failures = [var.s3_endpoint_url]
}

run "empty_secret_rejected" {
  command = plan
  variables {
    hydroshare_client_id     = "test-client"
    hydroshare_client_secret = ""
  }
  expect_failures = [var.hydroshare_client_secret]
}
