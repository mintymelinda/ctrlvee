#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

fail() {
    printf 'ERROR: %s\\n' "$*" >&2
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
command -v stty >/dev/null 2>&1 || fail "stty is required for hidden token entry."

cd "$PROJECT_DIR"
# Compose reads .env for interpolation, but the runner service is not given
# the VLC password as an environment variable.
$COMPOSE build actions-runner

printf '%s\\n' "Generate a short-lived repository runner token in GitHub:"
printf '%s\\n' "  Settings > Actions > Runners > New self-hosted runner"
printf '%s\\n' "Paste the token below. It is piped to the configure container over"
printf '%s\\n' "stdin; it is not placed in shell history or container environment."
printf 'GitHub runner registration token: '
stty -echo
if ! IFS= read -r RUNNER_TOKEN; then
    stty echo
    fail "Could not read the registration token."
fi
stty echo
printf '\\n'
[ -n "$RUNNER_TOKEN" ] || fail "The registration token cannot be empty."

if ! printf '%s\\n' "$RUNNER_TOKEN" | $COMPOSE run --rm -T --no-deps -e RUNNER_MODE=configure actions-runner; then
    unset RUNNER_TOKEN
    fail "Container runner configuration failed. The existing host runner has not been changed."
fi
unset RUNNER_TOKEN

$COMPOSE up -d actions-runner
printf '\\n%s\\n' "Container runner started. Confirm synology-vlc-lan-container is Idle/online in GitHub."
printf '%s\\n' "Only after confirming it works, stop the old DSM Task Scheduler runner and remove the old registration in GitHub."
