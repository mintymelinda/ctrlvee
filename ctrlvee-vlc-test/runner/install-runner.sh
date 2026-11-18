#!/bin/sh
set -eu

REPOSITORY_URL="https://github.com/mintymelinda/ctrlvee"
RUNNER_VERSION="2.338.2"
RUNNER_SHA256="baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40"
RUNNER_ARCHIVE="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNNER_DIR="${SCRIPT_DIR}/actions-runner"
TMP_ARCHIVE=""

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    stty echo >/dev/null 2>&1 || true
    if [ -n "$TMP_ARCHIVE" ]; then
        rm -f "$TMP_ARCHIVE"
    fi
    unset RUNNER_TOKEN
}

[ "$(id -u)" -ne 0 ] || fail "Run this installer as the dedicated, non-root DSM runner account."
[ "$(uname -m)" = "x86_64" ] || fail "This runner package targets x86_64; detected $(uname -m)."
command -v curl >/dev/null 2>&1 || fail "curl is required."
command -v tar >/dev/null 2>&1 || fail "tar is required."
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required."
command -v stty >/dev/null 2>&1 || fail "stty is required."
[ -t 0 ] && [ -t 1 ] || fail "Run this installer from an interactive SSH terminal."

if [ -e "${RUNNER_DIR}/.runner" ]; then
    fail "A runner is already configured in ${RUNNER_DIR}. Remove it from GitHub and configure it again only if you intend to replace that runner."
fi

printf '%s\n' "This installs the official GitHub Actions runner v${RUNNER_VERSION} for:"
printf '  %s\n' "$REPOSITORY_URL"
printf '%s\n' "It does not install a service or grant Docker access."
printf '%s\n' "Generate a short-lived registration token in GitHub:"
printf '%s\n' "  Repository Settings > Actions > Runners > New self-hosted runner"
printf '%s\n' "Paste that token below. It is used once and is not saved."
printf '\n'

trap cleanup EXIT
trap 'exit 1' HUP INT TERM

printf 'GitHub runner registration token: '
stty -echo
if ! IFS= read -r RUNNER_TOKEN; then
    fail "Could not read registration token."
fi
stty echo
printf '\n'
[ -n "$RUNNER_TOKEN" ] || fail "Registration token cannot be empty."

mkdir -p "$RUNNER_DIR"
TMP_ARCHIVE=$(mktemp "${SCRIPT_DIR}/.runner-download.XXXXXX")

printf 'Downloading official runner v%s...\n' "$RUNNER_VERSION"
curl --fail --location --silent --show-error --retry 3 \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_ARCHIVE}" \
    --output "$TMP_ARCHIVE"

printf '%s  %s\n' "$RUNNER_SHA256" "$TMP_ARCHIVE" | sha256sum --check --status \
    || fail "Runner archive checksum verification failed."

tar -xzf "$TMP_ARCHIVE" -C "$RUNNER_DIR"
rm -f "$TMP_ARCHIVE"
TMP_ARCHIVE=""

if command -v ldd >/dev/null 2>&1; then
    MISSING_LIBRARIES=$(ldd "$RUNNER_DIR/bin/Runner.Listener" 2>/dev/null | grep "not found" || true)
    [ -z "$MISSING_LIBRARIES" ] || fail "The Synology host is missing runner libraries: $MISSING_LIBRARIES"
fi

if command -v ldd >/dev/null 2>&1; then
    MISSING_LIBRARIES=$(ldd "$RUNNER_DIR/bin/Runner.Listener" 2>/dev/null | grep "not found" || true)
    [ -z "$MISSING_LIBRARIES" ] || fail "The Synology host is missing runner libraries: $MISSING_LIBRARIES"
fi

cd "$RUNNER_DIR"
./config.sh \
    --url "$REPOSITORY_URL" \
    --token "$RUNNER_TOKEN" \
    --name "synology-vlc-lan" \
    --labels "vlc-lan" \
    --work "_work" \
    --unattended

unset RUNNER_TOKEN

printf '\nRunner configured successfully. Start it with:\n'
printf '  %s\n' "${SCRIPT_DIR}/run-runner.sh"
printf '\nFor automatic startup, add that command to DSM Task Scheduler as a\n'
printf '%s\n' "Triggered Task > User-defined script, running as this same account."
