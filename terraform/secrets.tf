resource "google_secret_manager_secret" "celery_broker" {
  secret_id = "${local.name}-celery-broker-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "celery_broker" {
  secret      = google_secret_manager_secret.celery_broker.id
  secret_data = local.redis_broker_url
}

resource "google_secret_manager_secret" "celery_result_backend" {
  secret_id = "${local.name}-celery-result-backend"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "celery_result_backend" {
  secret      = google_secret_manager_secret.celery_result_backend.id
  secret_data = local.redis_result_store
}

resource "google_secret_manager_secret" "oauth" {
  for_each  = toset(["client-id", "client-secret"])
  secret_id = "${local.name}-hydroshare-${each.key}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
  lifecycle {
    precondition {
      condition     = (var.hydroshare_client_id == null) == (var.hydroshare_client_secret == null)
      error_message = "Supply both hydroshare_client_id and hydroshare_client_secret, or neither for external secret versions."
    }
  }
}

locals {
  # Only presence is declassified; credential contents remain sensitive.
  manage_oauth_versions = nonsensitive(var.hydroshare_client_id != null)
  oauth_credentials = {
    client-id     = var.hydroshare_client_id
    client-secret = var.hydroshare_client_secret
  }
}

resource "google_secret_manager_secret_version" "oauth" {
  for_each    = local.manage_oauth_versions ? toset(["client-id", "client-secret"]) : toset([])
  secret      = google_secret_manager_secret.oauth[each.key].id
  secret_data = local.oauth_credentials[each.key]
}
