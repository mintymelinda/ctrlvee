#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    fail "Docker Compose V2 (docker compose) or legacy docker-compose is required."
fi

[ -f "$PROJECT_DIR/.env" ] || fail "Missing $PROJECT_DIR/.env. Create it from .env.example and set VLC_TEST_PASSWORD first."
[ -d "$PROJECT_DIR/media" ] || fail "Missing media directory: $PROJECT_DIR/media"
[ -f "$PROJECT_DIR/../Dockerfile.actions-runner" ] || fail "Missing repository-root Dockerfile.actions-runner."
[ -f "$SCRIPT_DIR/container-entrypoint.sh" ] || fail "Missing runner container entrypoint."
command -v stty >/dev/null 2>&1 || fail "stty is required for hidden token entry."
command -v id >/dev/null 2>&1 || fail "id is required to map the runner account to NAS media ACLs."
command -v mktemp >/dev/null 2>&1 || fail "mktemp is required to create an isolated Docker build context."
id ctrlvee-runner >/dev/null 2>&1 || fail "The existing ctrlvee-runner DSM account is required; run the previous account setup first."
RUNNER_UID=$(id -u ctrlvee-runner)
RUNNER_GID=$(id -g ctrlvee-runner)
export RUNNER_UID RUNNER_GID

cd "$PROJECT_DIR"

# Docker 20.10 on DSM attempts to stat inaccessible host-runner paths before
# applying .dockerignore. Build from a minimal temporary context instead:
# this image needs only its Dockerfile and the container entrypoint script.
BUILD_CONTEXT=$(mktemp -d "${TMPDIR:-/tmp}/ctrlvee-runner-build.XXXXXX")
cleanup_build_context() {
    rm -rf "$BUILD_CONTEXT"
}
trap cleanup_build_context EXIT HUP INT TERM

mkdir -p "$BUILD_CONTEXT/ctrlvee-vlc-test/runner"
cp "$PROJECT_DIR/../Dockerfile.actions-runner" "$BUILD_CONTEXT/Dockerfile.actions-runner"
cp "$SCRIPT_DIR/container-entrypoint.sh" "$BUILD_CONTEXT/ctrlvee-vlc-test/runner/container-entrypoint.sh"

printf '%s\n' "Building actions-runner from an isolated context..."
docker build \
    -f "$BUILD_CONTEXT/Dockerfile.actions-runner" \
    --build-arg "RUNNER_UID=$RUNNER_UID" \
    --build-arg "RUNNER_GID=$RUNNER_GID" \
    -t ctrlvee-actions-runner:local \
    "$BUILD_CONTEXT"

cleanup_build_context
trap - EXIT HUP INT TERM

printf '%s\n' "Generate a short-lived repository runner token in GitHub:"
printf '%s\n' "  Settings > Actions > Runners > New self-hosted runner"
printf '%s\n' "Paste the token below. It is piped to the configure container over"
printf '%s\n' "stdin; it is not placed in shell history or container environment."
printf 'GitHub runner registration token: '
stty -echo
if ! IFS= read -r RUNNER_TOKEN; then
    stty echo
    fail "Could not read the registration token."
fi
stty echo
printf '\n'
[ -n "$RUNNER_TOKEN" ] || fail "The registration token cannot be empty."

if ! printf '%s\n' "$RUNNER_TOKEN" | $COMPOSE run --rm -T --no-deps -e RUNNER_MODE=configure actions-runner; then
    unset RUNNER_TOKEN
    fail "Container runner configuration failed. The existing host runner has not been changed."
fi
unset RUNNER_TOKEN

$COMPOSE up -d actions-runner
printf '\n%s\n' "Container runner started. Confirm synology-vlc-lan-container is Idle/online in GitHub."
printf '%s\n' "Only after confirming it works, stop the old DSM Task Scheduler runner and remove the old registration in GitHub."
