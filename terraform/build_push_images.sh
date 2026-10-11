#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: bash %s IMAGE_TAG\nBuild and push API and worker images to the Terraform-created Artifact Registry.\n' "$0"
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi
if [[ $# -ne 1 || ! "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
    usage >&2
    printf 'ERROR: Supply one valid Docker image tag (for example, release-20261011).\n' >&2
    exit 2
fi

for tool in terraform gcloud docker; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'ERROR: Required command is unavailable: %s\n' "$tool" >&2
        exit 1
    fi
done

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if ! registry="$(terraform "-chdir=$root/terraform" output -raw registry_url)"; then
    printf 'ERROR: Cannot read registry_url. Initialize and apply the Terraform bootstrap first.\n' >&2
    exit 1
fi
if [[ ! "$registry" =~ ^[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9._:-]+/[a-z0-9._-]+$ ]]; then
    printf 'ERROR: registry_url is not a Google Artifact Registry repository address.\n' >&2
    exit 1
fi

api_image="$registry/api:$1"
worker_image="$registry/worker:$1"

docker info >/dev/null
gcloud auth configure-docker "${registry%%/*}" --quiet

docker build --platform linux/amd64 \
    -f "$root/docker/api/Dockerfile" -t "$api_image" "$root"
docker build --platform linux/amd64 \
    -f "$root/docker/api/worker.Dockerfile" -t "$worker_image" "$root"

docker push "$api_image"
docker push "$worker_image"

printf '\nBoth images pushed. Set these values in terraform/terraform.tfvars:\n'
printf 'deploy_services = true\napi_image = "%s"\nworker_image = "%s"\n' "$api_image" "$worker_image"
