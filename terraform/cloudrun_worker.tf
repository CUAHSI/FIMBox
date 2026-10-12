resource "google_cloud_run_v2_worker_pool" "worker" {
  provider            = google-beta
  count               = var.deploy_services ? 1 : 0
  name                = "${local.name}-worker"
  location            = var.region
  deletion_protection = false

  lifecycle {
    precondition {
      condition     = var.worker_image != ""
      error_message = "Push the HAND-FIM worker image and set worker_image before enabling deploy_services."
    }
  }

  template {
    service_account = google_service_account.worker.email
    vpc_access {
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.subnet.id
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.worker_image
      resources {
        limits = { cpu = var.worker_cpu, memory = var.worker_memory }
      }
      dynamic "env" {
        for_each = merge(local.common_environment, {
          APP_ROLE           = "worker"
          HAND_FIM_MAX_PROCS = tostring(var.hand_fim_max_procs)
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
    }
  }
  scaling {
    manual_instance_count = var.worker_instance_count
  }
  depends_on = [
    google_secret_manager_secret_iam_member.queue_access,
    google_artifact_registry_repository_iam_member.runtime_reader,
  ]
}
