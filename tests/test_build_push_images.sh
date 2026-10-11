#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
directory="$(mktemp -d)"
trap 'rm -f "$directory/calls" "$directory/output"; rmdir "$directory"' EXIT
export CALL_LOG="$directory/calls"
export TEST_REGISTRY="us-central1-docker.pkg.dev/test-project/fimbox-dev"

terraform() {
    printf 'terraform %s\n' "$*" >> "$CALL_LOG"
    if [[ "${FAIL_STEP:-}" == "terraform" ]]; then return 1; fi
    printf '%s' "$TEST_REGISTRY"
}
gcloud() {
    printf 'gcloud %s\n' "$*" >> "$CALL_LOG"
}
docker() {
    printf 'docker %s\n' "$*" >> "$CALL_LOG"
    if [[ "${FAIL_STEP:-}" == "build" && "$1" == "build" ]]; then return 1; fi
    if [[ "${FAIL_STEP:-}" == "push" && "$1" == "push" ]]; then return 1; fi
}
export -f terraform gcloud docker

bash -n "$root/terraform/build_push_images.sh"
cd "$directory"
bash "$root/terraform/build_push_images.sh" release-test > "$directory/output"
grep -Fq 'gcloud auth configure-docker us-central1-docker.pkg.dev --quiet' "$CALL_LOG"
grep -Fq "docker build --platform linux/amd64 -f $root/docker/api/Dockerfile -t $TEST_REGISTRY/api:release-test $root" "$CALL_LOG"
grep -Fq "docker build --platform linux/amd64 -f $root/docker/api/worker.Dockerfile -t $TEST_REGISTRY/worker:release-test $root" "$CALL_LOG"
grep -Fq "api_image = \"$TEST_REGISTRY/api:release-test\"" "$directory/output"
grep -Fq "worker_image = \"$TEST_REGISTRY/worker:release-test\"" "$directory/output"
[[ "$(grep -c '^docker push ' "$CALL_LOG")" == 2 ]]
[[ "$(tail -n 2 "$CALL_LOG")" == "$(printf 'docker push %s/api:release-test\ndocker push %s/worker:release-test' "$TEST_REGISTRY" "$TEST_REGISTRY")" ]]

for step in terraform build push; do
    : > "$CALL_LOG"
    if FAIL_STEP="$step" bash "$root/terraform/build_push_images.sh" release-test > "$directory/output" 2>&1; then
        printf 'ERROR: Expected failure at %s\n' "$step" >&2
        exit 1
    fi
    ! grep -q 'Both images pushed' "$directory/output"
    if [[ "$step" != push ]]; then
        ! grep -q '^docker push ' "$CALL_LOG"
    else
        [[ "$(grep -c '^docker push ' "$CALL_LOG")" == 1 ]]
    fi
done

: > "$CALL_LOG"
if TEST_REGISTRY="invalid-registry" bash "$root/terraform/build_push_images.sh" release-test > "$directory/output" 2>&1; then
    exit 1
fi
! grep -q '^docker ' "$CALL_LOG"
: > "$CALL_LOG"
if bash "$root/terraform/build_push_images.sh" 'bad/tag' > "$directory/output" 2>&1; then
    exit 1
fi
[[ ! -s "$CALL_LOG" ]]
printf 'Build/push script tests passed (mocked commands; no images pushed).\n'
