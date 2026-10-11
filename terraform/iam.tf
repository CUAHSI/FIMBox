resource "google_service_account" "fastapi" {
  account_id   = "${local.name}-api"
  display_name = "${local.name} FastAPI runtime"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "worker" {
  account_id   = "${local.name}-worker"
  display_name = "${local.name} Celery worker runtime"
  depends_on   = [google_project_service.apis]
}

locals {
  runtime_accounts = {
    api    = google_service_account.fastapi.email
    worker = google_service_account.worker.email
  }
  queue_secret_access = {
    api_broker    = { account = local.runtime_accounts.api, secret = google_secret_manager_secret.celery_broker.id }
    api_result    = { account = local.runtime_accounts.api, secret = google_secret_manager_secret.celery_result_backend.id }
    worker_broker = { account = local.runtime_accounts.worker, secret = google_secret_manager_secret.celery_broker.id }
    worker_result = { account = local.runtime_accounts.worker, secret = google_secret_manager_secret.celery_result_backend.id }
  }
}

resource "google_artifact_registry_repository_iam_member" "runtime_reader" {
  for_each   = local.runtime_accounts
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${each.value}"
}

resource "google_secret_manager_secret_iam_member" "queue_access" {
  for_each  = local.queue_secret_access
  secret_id = each.value.secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.account}"
}

resource "google_secret_manager_secret_iam_member" "oauth_access" {
  for_each  = google_secret_manager_secret.oauth
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.fastapi.email}"
}
