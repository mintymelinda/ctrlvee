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
if [ -e "${RUNNER_DIR}/.runner" ]; then
    fail "A runner is already configured in ${RUNNER_DIR}. Remove it from GitHub and configure it again only if you intend to replace that runner."
fi
command -v curl >/dev/null 2>&1 || fail "curl is required."
command -v tar >/dev/null 2>&1 || fail "tar is required."
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required."
command -v stty >/dev/null 2>&1 || fail "stty is required."
command -v grep >/dev/null 2>&1 || fail "grep is required by GitHub runner configuration."
command -v bash >/dev/null 2>&1 || fail "bash is required by GitHub runner configuration."
command -v readlink >/dev/null 2>&1 || fail "readlink is required by GitHub runner configuration."
command -v dirname >/dev/null 2>&1 || fail "dirname is required by GitHub runner configuration."
command -v pwd >/dev/null 2>&1 || fail "pwd is required by GitHub runner configuration."
command -v rm >/dev/null 2>&1 || fail "rm is required."
command -v mktemp >/dev/null 2>&1 || fail "mktemp is required."
command -v touch >/dev/null 2>&1 || fail "touch is required by GitHub runner configuration."
[ -t 0 ] && [ -t 1 ] || fail "Run this installer from an interactive SSH terminal."

# DSM may provide glibc and its dynamic loader without shipping ldd or
# ldconfig. GitHub's config.sh requires both commands. Keep compatibility
# helpers private to this runner instead of modifying the DSM host.
NEED_LOCAL_TOOLS=0
if ! command -v ldd >/dev/null 2>&1; then
    [ -r "${SCRIPT_DIR}/ldd-compat.sh" ] || fail "DSM has no ldd command and the bundled ldd-compat.sh helper is missing."
    NEED_LOCAL_TOOLS=1
fi
if ! [ -x "$(command -v ldconfig 2>/dev/null)" ] && [ ! -x /sbin/ldconfig ]; then
    [ -r "${SCRIPT_DIR}/ldconfig-compat.sh" ] || fail "DSM has no ldconfig command and the bundled ldconfig-compat.sh helper is missing."
    NEED_LOCAL_TOOLS=1
fi

if [ "$NEED_LOCAL_TOOLS" -eq 1 ]; then
    mkdir -p "${RUNNER_DIR}/.local-tools"
fi
if ! command -v ldd >/dev/null 2>&1; then
    cat "${SCRIPT_DIR}/ldd-compat.sh" > "${RUNNER_DIR}/.local-tools/ldd"
    chmod 700 "${RUNNER_DIR}/.local-tools/ldd"
    printf '%s\n' "DSM has no system ldd; using the runner-local glibc dynamic-loader compatibility helper."
fi
if ! [ -x "$(command -v ldconfig 2>/dev/null)" ] && [ ! -x /sbin/ldconfig ]; then
    cat "${SCRIPT_DIR}/ldconfig-compat.sh" > "${RUNNER_DIR}/.local-tools/ldconfig"
    chmod 700 "${RUNNER_DIR}/.local-tools/ldconfig"
    printf '%s\n' "DSM has no system ldconfig; using a runner-local, read-only ICU discovery helper."
fi
if [ -d "${RUNNER_DIR}/.local-tools" ]; then
    PATH="${RUNNER_DIR}/.local-tools:${PATH}"
    export PATH
fi
command -v ldd >/dev/null 2>&1 || fail "Could not provide ldd for GitHub runner configuration."
if ! [ -x "$(command -v ldconfig 2>/dev/null)" ] && [ ! -x /sbin/ldconfig ]; then
    fail "Could not provide ldconfig for GitHub runner configuration."
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
TMP_ARCHIVE=$(mktemp "${RUNNER_DIR}/.runner-download.XXXXXX")

printf 'Downloading official runner v%s...\n' "$RUNNER_VERSION"
curl --fail --location --silent --show-error --retry 3 \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_ARCHIVE}" \
    --output "$TMP_ARCHIVE"

printf '%s  %s\n' "$RUNNER_SHA256" "$TMP_ARCHIVE" | sha256sum --check --status \
    || fail "Runner archive checksum verification failed."

tar -xzf "$TMP_ARCHIVE" -C "$RUNNER_DIR"
rm -f "$TMP_ARCHIVE"
TMP_ARCHIVE=""

MISSING_LIBRARIES=$(ldd "$RUNNER_DIR/bin/Runner.Listener" 2>/dev/null | grep "not found" || true)
[ -z "$MISSING_LIBRARIES" ] || fail "The Synology host is missing runner libraries: $MISSING_LIBRARIES"

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
