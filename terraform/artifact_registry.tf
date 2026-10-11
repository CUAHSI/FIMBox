resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = local.name
  description   = "FIMBox API and HAND-FIM worker images"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}
