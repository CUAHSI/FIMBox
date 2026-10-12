variable "project_id" {
  type        = string
  description = "Google Cloud project with billing enabled."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "name_prefix" {
  type    = string
  default = "fimbox"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "deploy_services" {
  type        = bool
  default     = false
  description = "Enable after pushing the API/worker images and adding OAuth secret versions."
}

variable "api_image" {
  type        = string
  default     = ""
  description = "Linux/amd64 API image in the provisioned Artifact Registry repository; prefer a digest."
}

variable "worker_image" {
  type        = string
  default     = ""
  description = "Linux/amd64 HAND-FIM worker image; different from the slim API image."
}

variable "public_api_url" {
  type        = string
  default     = "https://configure-before-login.invalid"
  description = "HTTPS API origin for both registered OAuth callbacks. Update after the first service deployment."
  validation {
    condition     = can(regex("^https://[^/]+$", var.public_api_url))
    error_message = "public_api_url must be an HTTPS origin without a trailing slash or path."
  }
}

variable "oauth_secret_version" {
  type        = string
  default     = "1"
  description = "Existing numeric version used when OAuth credentials are populated outside Terraform."
  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.oauth_secret_version))
    error_message = "Pin a numeric secret version rather than latest."
  }
}

variable "hydroshare_client_id" {
  type        = string
  default     = null
  sensitive   = true
  description = "Optional OAuth client ID. Supply with hydroshare_client_secret to manage Secret Manager versions. Stored in state."
  validation {
    condition     = var.hydroshare_client_id == null ? true : trimspace(var.hydroshare_client_id) != ""
    error_message = "hydroshare_client_id must be nonempty or null for externally managed credentials."
  }
}

variable "hydroshare_client_secret" {
  type        = string
  default     = null
  sensitive   = true
  description = "Optional plaintext OAuth client secret. Supply with hydroshare_client_id. Sensitive but stored in Terraform state."
  validation {
    condition     = var.hydroshare_client_secret == null ? true : trimspace(var.hydroshare_client_secret) != ""
    error_message = "hydroshare_client_secret must be nonempty or null for externally managed credentials."
  }
}

variable "s3_endpoint_url" {
  type        = string
  default     = "https://s3.hydroshare.org"
  description = "HTTPS S3-compatible endpoint used by both API signing and worker uploads; must accept HydroShare-issued credentials."
  validation {
    condition     = can(regex("^https://[^/?#]+/?$", var.s3_endpoint_url))
    error_message = "s3_endpoint_url must be an HTTPS origin without a path, query, or fragment."
  }
}

variable "api_max_instances" {
  type    = number
  default = 5
}

variable "api_min_instances" {
  type        = number
  default     = 0
  description = "Minimum warm API instances; zero permits scale-to-zero."
  validation {
    condition     = var.api_min_instances >= 0 && floor(var.api_min_instances) == var.api_min_instances
    error_message = "api_min_instances must be a nonnegative integer."
  }
}

variable "api_concurrency" {
  type        = number
  default     = 80
  description = "Maximum concurrent requests per API instance."
  validation {
    condition     = var.api_concurrency >= 1 && var.api_concurrency <= 1000 && floor(var.api_concurrency) == var.api_concurrency
    error_message = "api_concurrency must be an integer between 1 and 1000."
  }
}

variable "api_timeout_seconds" {
  type        = number
  default     = 60
  description = "Cloud Run API request timeout; not the scientific job runtime limit."
  validation {
    condition     = var.api_timeout_seconds >= 1 && var.api_timeout_seconds <= 3600 && floor(var.api_timeout_seconds) == var.api_timeout_seconds
    error_message = "api_timeout_seconds must be an integer between 1 and 3600."
  }
}

variable "worker_instance_count" {
  type    = number
  default = 1
}

variable "worker_cpu" {
  type    = string
  default = "4"
}

variable "worker_memory" {
  type        = string
  default     = "16Gi"
  description = "HAND-FIM scratch files and scientific computation both consume Cloud Run memory."
}

variable "hand_fim_max_procs" {
  type    = number
  default = 1
  validation {
    condition     = var.hand_fim_max_procs >= 1 && floor(var.hand_fim_max_procs) == var.hand_fim_max_procs
    error_message = "hand_fim_max_procs must be a positive integer."
  }
}

variable "redis_memory_gb" {
  type    = number
  default = 1
}

variable "result_ttl_seconds" {
  type        = number
  default     = 86400
  description = "Result, ownership, credentials, and broker visibility TTL. Must exceed maximum job runtime."
}
