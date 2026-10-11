resource "google_redis_instance" "celery" {
  name               = "${local.name}-redis"
  region             = var.region
  tier               = "BASIC"
  memory_size_gb     = var.redis_memory_gb
  redis_version      = "REDIS_7_2"
  authorized_network = google_compute_network.vpc.id
  connect_mode       = "PRIVATE_SERVICE_ACCESS"
  redis_configs      = { maxmemory-policy = "noeviction" }
  depends_on         = [google_service_networking_connection.psa]
}
