resource "google_cloud_run_v2_service" "api" {
  count               = var.deploy_services ? 1 : 0
  name                = "${local.name}-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  lifecycle {
    precondition {
      condition     = var.api_image != ""
      error_message = "Push the API image and set api_image before enabling deploy_services."
    }
    precondition {
      condition     = var.api_min_instances <= var.api_max_instances
      error_message = "api_min_instances must not exceed api_max_instances."
    }
  }

  template {
    service_account                  = google_service_account.fastapi.email
    timeout                          = "${var.api_timeout_seconds}s"
    max_instance_request_concurrency = var.api_concurrency
    scaling {
      min_instance_count = var.api_min_instances
      max_instance_count = var.api_max_instances
    }
    vpc_access {
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.subnet.id
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.api_image
      ports {
        container_port = 8000
      }
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
      dynamic "env" {
        for_each = merge(local.common_environment, local.stream_environment, local.stream_queue == "" ? {} : {
          STREAM_QUEUE = local.stream_queue
          }, {
          APP_ROLE                      = "api"
          REQUIRE_HYDROSHARE_OUTPUT     = "true"
          OAUTH_STATE_COOKIE_SECURE     = "true"
          HYDROSHARE_OAUTH_REDIRECT_URI = "${var.public_api_url}/auth/callback"
          SWAGGER_OAUTH_REDIRECT_URI    = "${var.public_api_url}/docs/oauth2-redirect"
        })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = {
          CELERY_BROKER_URL     = google_secret_manager_secret_version.celery_broker
          CELERY_RESULT_BACKEND = google_secret_manager_secret_version.celery_result_backend
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = env.value.version
            }
          }
        }
      }
      dynamic "env" {
        for_each = {
          HYDROSHARE_CLIENT_ID     = google_secret_manager_secret.oauth["client-id"].secret_id
          HYDROSHARE_CLIENT_SECRET = google_secret_manager_secret.oauth["client-secret"].secret_id
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret = env.value
              version = local.manage_oauth_versions ? google_secret_manager_secret_version.oauth[
                env.key == "HYDROSHARE_CLIENT_ID" ? "client-id" : "client-secret"
              ].version : var.oauth_secret_version
            }
          }
        }
      }
    }
  }
  traffic {
    percent = 100
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
  }
  depends_on = [
    google_secret_manager_secret_iam_member.queue_access,
    google_secret_manager_secret_iam_member.oauth_access,
    google_artifact_registry_repository_iam_member.runtime_reader,
  ]
}

# Browser OAuth requires public HTTP access; the application enforces HydroShare sessions.
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  count    = var.deploy_services ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
