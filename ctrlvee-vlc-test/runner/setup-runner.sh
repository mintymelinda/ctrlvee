#!/bin/sh
set -eu

RUNNER_USER="ctrlvee-runner"
MEDIA_DIR="/volume1/docker/ctrlvee-vlc-test/media"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNNER_DIR="${SCRIPT_DIR}/actions-runner"
VLC_ENV="$(dirname "$MEDIA_DIR")/.env"

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail "Run this setup with sudo: sudo sh $0"
[ "$(uname -m)" = "x86_64" ] || fail "This runner package targets x86_64; detected $(uname -m)."
[ -d "$MEDIA_DIR" ] || fail "Test media directory does not exist: $MEDIA_DIR"
command -v openssl >/dev/null 2>&1 || fail "openssl is required to generate an unguessable, unused account password."
command -v find >/dev/null 2>&1 || fail "find is required."
command -v su >/dev/null 2>&1 || fail "su is required."
command -v chown >/dev/null 2>&1 || fail "chown is required."

SYNOUSER=$(command -v synouser 2>/dev/null || true)
SYNOACLTOOL=$(command -v synoacltool 2>/dev/null || true)
[ -n "$SYNOUSER" ] || [ ! -x /usr/syno/sbin/synouser ] || SYNOUSER="/usr/syno/sbin/synouser"
[ -n "$SYNOACLTOOL" ] || [ ! -x /usr/syno/bin/synoacltool ] || SYNOACLTOOL="/usr/syno/bin/synoacltool"
[ -n "$SYNOUSER" ] || fail "DSM synouser was not found. This setup is for Synology DSM."
[ -n "$SYNOACLTOOL" ] || fail "DSM synoacltool was not found. Cannot safely grant media access."

if "$SYNOUSER" --get "$RUNNER_USER" >/dev/null 2>&1; then
    USER_INFO=$("$SYNOUSER" --get "$RUNNER_USER") || fail "Could not inspect existing $RUNNER_USER account."
    printf '%s\n' "$USER_INFO" | grep -q 'AUTH_LOCAL' || fail "$RUNNER_USER exists but is not a local DSM account."
    if printf '%s\n' "$USER_INFO" | grep -qi 'administrators'; then
        fail "$RUNNER_USER is in the administrators group; refusing to use a privileged account."
    fi
    printf 'Using existing non-administrator DSM account: %s\n' "$RUNNER_USER"
else
    ACCOUNT_PASSWORD=$(openssl rand -hex 32)
    "$SYNOUSER" --add "$RUNNER_USER" "$ACCOUNT_PASSWORD" "CtrlVee GitHub Actions Runner" 0 "" 0 \
        || fail "DSM could not create the $RUNNER_USER account."
    unset ACCOUNT_PASSWORD
    USER_INFO=$("$SYNOUSER" --get "$RUNNER_USER") || fail "DSM created the account but could not verify it."
    if printf '%s\n' "$USER_INFO" | grep -qi 'administrators'; then
        fail "New runner account unexpectedly belongs to administrators."
    fi
    printf 'Created dedicated DSM account: %s\n' "$RUNNER_USER"
fi

ensure_acl() {
    path=$1
    entry=$2
    current=$("$SYNOACLTOOL" -get "$path" 2>/dev/null) || fail "Could not inspect ACL on $path."
    case "$current" in
        *"$entry"*) ;;
        *) "$SYNOACLTOOL" -add "$path" "$entry" >/dev/null || fail "Could not apply runner ACL to $path." ;;
    esac
}

# Permit path traversal without allowing directory listings on parent folders.
for start in "$SCRIPT_DIR" "$MEDIA_DIR"; do
    path=$(dirname "$start")
    while [ "$path" != "/" ]; do
        [ -d "$path" ] || fail "Expected parent directory is missing: $path"
        ensure_acl "$path" "user:$RUNNER_USER:deny:r-----------:----"
        ensure_acl "$path" "user:$RUNNER_USER:allow:--x----------:----"
        path=$(dirname "$path")
    done
done

# The runner can list/traverse its scripts directory, but cannot change the
# checked-out repository. Its ignored actions-runner directory is separate.
ensure_acl "$SCRIPT_DIR" "user:$RUNNER_USER:allow:r-x----------:fd--"

# Grant read-only access to fixture directories and files. The directory ACE
# propagates to future fixtures added below this directory.
find "$MEDIA_DIR" -type d -exec sh -c '
    tool=$1
    user=$2
    shift 2
    for path do
        current=$("$tool" -get "$path" 2>/dev/null) || exit 1
        case "$current" in
            *"user:$user:allow:"*) ;;
            *) "$tool" -add "$path" "user:$user:allow:r-x----------:fd--" >/dev/null || exit 1 ;;
        esac
    done
' sh "$SYNOACLTOOL" "$RUNNER_USER" {} + || fail "Could not grant read/traverse access to media directories."

find "$MEDIA_DIR" -type f -exec sh -c '
    tool=$1
    user=$2
    shift 2
    for path do
        current=$("$tool" -get "$path" 2>/dev/null) || exit 1
        case "$current" in
            *"user:$user:allow:"*) ;;
            *) "$tool" -add "$path" "user:$user:allow:r-----------:----" >/dev/null || exit 1 ;;
        esac
    done
' sh "$SYNOACLTOOL" "$RUNNER_USER" {} + || fail "Could not grant read-only access to existing media files."

# The local VLC password is not part of the runner's required inputs.
if [ -f "$VLC_ENV" ]; then
    ensure_acl "$VLC_ENV" "user:$RUNNER_USER:deny:r-----------:----"
fi

mkdir -p "$RUNNER_DIR"
chown -R "$RUNNER_USER" "$RUNNER_DIR"
chmod 700 "$RUNNER_DIR"

run_as_runner() {
    su -s /bin/sh -c "$1" "$RUNNER_USER"
}

# Fail closed if the account can browse parent directories or read the VLC
# password. Check both directory access and read-only media access as the user.
for start in "$SCRIPT_DIR" "$MEDIA_DIR"; do
    path=$(dirname "$start")
    while [ "$path" != "/" ]; do
        run_as_runner "test ! -r '$path' && test -x '$path'" \
            || fail "Runner account can list or cannot traverse $path; inspect its effective DSM ACLs."
        path=$(dirname "$path")
    done
done

run_as_runner "test -r '$SCRIPT_DIR/install-runner.sh' && test -x '$SCRIPT_DIR' && test -w '$RUNNER_DIR'" \
    || fail "Runner account cannot read the installer or write its private runner directory."
run_as_runner "test -r '$MEDIA_DIR' && test -x '$MEDIA_DIR' && ! test -w '$MEDIA_DIR'" \
    || fail "Runner account does not have strictly read-only access to the media directory."
if [ -S /var/run/docker.sock ]; then
    run_as_runner "test ! -r /var/run/docker.sock && test ! -w /var/run/docker.sock" \
        || fail "Runner account can access the Docker socket; remove that permission before continuing."
fi
if [ -f "$VLC_ENV" ]; then
    run_as_runner "test ! -r '$VLC_ENV'" \
        || fail "Runner account can read $VLC_ENV. Restrict that file before continuing."
fi

if [ -f "$RUNNER_DIR/.runner" ]; then
    printf '\nRunner is already configured. No new GitHub registration was made.\n'
else
    printf '\nInstalling the official GitHub Actions runner as %s...\n' "$RUNNER_USER"
    run_as_runner "exec /bin/sh '$SCRIPT_DIR/install-runner.sh'"
fi

printf '\nSetup complete. Runner account: %s\n' "$RUNNER_USER"
printf 'Start it manually with: sudo -u %s /bin/sh %s/run-runner.sh\n' "$RUNNER_USER" "$SCRIPT_DIR"
printf 'For automatic startup, create a DSM boot task running as %s.\n' "$RUNNER_USER"
