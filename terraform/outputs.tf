output "registry_url" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "api_url" {
  value = try(google_cloud_run_v2_service.api[0].uri, null)
}

output "worker_pool_name" {
  value = try(google_cloud_run_v2_worker_pool.worker[0].name, null)
}

output "stream_worker_pool_name" {
  value = try(google_cloud_run_v2_worker_pool.stream_worker[0].name, null)
}

output "oauth_secret_names" {
  value = { for key, secret in google_secret_manager_secret.oauth : key => secret.secret_id }
}

output "oauth_client_id_secret" {
  value = google_secret_manager_secret.oauth["client-id"].secret_id
}

output "oauth_client_secret_secret" {
  value = google_secret_manager_secret.oauth["client-secret"].secret_id
}
