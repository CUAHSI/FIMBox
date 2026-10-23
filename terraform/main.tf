# Adapted from CUAHSI/fimbox-api b6ddd220bd72c950cfa16a6f76bf3de3a54acd11.
provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  name               = "${var.name_prefix}-${var.environment}"
  redis_broker_url   = "redis://${google_redis_instance.celery.host}:${google_redis_instance.celery.port}/0"
  redis_result_store = "redis://${google_redis_instance.celery.host}:${google_redis_instance.celery.port}/1"
  redis_auth_url     = "redis://${google_redis_instance.celery.host}:${google_redis_instance.celery.port}/2"
  common_environment = {
    APP_ENV                    = var.environment
    REDIS_URL                  = local.redis_auth_url
    CELERY_TASK_DEFAULT_QUEUE  = "default"
    RESULT_TTL_SECONDS         = tostring(var.result_ttl_seconds)
    VISIBILITY_TIMEOUT_SECONDS = tostring(var.result_ttl_seconds)
    S3_ENDPOINT_URL            = var.s3_endpoint_url
    S3_REGION                  = "us-east-1"
    S3_ADDRESSING_STYLE        = "path"
  }
  stream_environment = var.stream_input_bucket == "" ? {} : {
    STREAM_INPUT_BUCKET   = var.stream_input_bucket
    STREAM_INPUT_PREFIX   = var.stream_input_prefix
    STREAM_INPUT_UNSIGNED = tostring(var.stream_input_unsigned)
  }
  stream_queue = var.stream_worker_instance_count > 0 ? "streaming" : ""
}

resource "google_project_service" "apis" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "redis.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
