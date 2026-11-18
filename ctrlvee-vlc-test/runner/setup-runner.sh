#!/bin/sh
set -eu

RUNNER_USER="ctrlvee-runner"
MEDIA_DIR="/volume1/docker/ctrlvee-vlc-test/media"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNNER_DIR="$SCRIPT_DIR/actions-runner"
VLC_ENV="$(dirname "$MEDIA_DIR")/.env"

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail "Run this setup with sudo: sudo sh $0"
[ "$(uname -m)" = "x86_64" ] || fail "This runner package targets x86_64; detected $(uname -m)."
for command_name in id uname dirname mkdir chmod awk grep sed head ls; do
    command -v "$command_name" >/dev/null 2>&1 || fail "$command_name is required by the DSM account and ACL setup."
done
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

# Remove only this runner user's explicit ACEs. Old versions of this installer
# added inherited DENY entries on parent folders; those DENYs also blocked
# access to the media subtree. Inherited ACEs are not deleted by this command.
remove_runner_acl() {
    path=$1
    while :; do
        current=$("$SYNOACLTOOL" -get "$path" 2>/dev/null || true)
        index=$(printf '%s\n' "$current" | awk -v entry="user:$RUNNER_USER:" '
            index($0, entry) && index($0, "(level:0)") {
                token=$1
                gsub(/[^0-9]/, "", token)
                print token
                exit
            }
        ')
        [ -n "$index" ] || break
        "$SYNOACLTOOL" -del "$path" "$index" >/dev/null \
            || fail "Could not remove a previous runner ACL entry from $path."
    done
}

ensure_acl() {
    path=$1
    entry=$2
    current=$("$SYNOACLTOOL" -get "$path" 2>/dev/null || true)
    case "$current" in
        *"$entry"*) ;;
        *) "$SYNOACLTOOL" -add "$path" "$entry" >/dev/null || fail "Could not apply runner ACL to $path." ;;
    esac
}

# Remove legacy runner ACEs from every parent in both paths. Do not install
# DENY ACEs on ancestors: DSM inheritance can carry them into the media tree,
# where they override the intended fixture-read permission.
for start in "$SCRIPT_DIR" "$MEDIA_DIR"; do
    path=$(dirname "$start")
    while [ "$path" != "/" ]; do
        [ -d "$path" ] || fail "Expected parent directory is missing: $path"
        remove_runner_acl "$path"
        ensure_acl "$path" "user:$RUNNER_USER:allow:--x----------:----"
        path=$(dirname "$path")
    done
done

# The runner needs to read its scripts. Its private actions-runner directory
# is separate and is owned by the runner account.
remove_runner_acl "$SCRIPT_DIR"
ensure_acl "$SCRIPT_DIR" "user:$RUNNER_USER:allow:r-x----------:fd--"

# Synology creates @eaDir trees containing metadata/stream files. They are
# not media fixtures and must not be granted to or included in runner checks.
# Remove legacy ACEs and grant read/traverse only on real media directories.
find "$MEDIA_DIR" \
    \( -name '@eaDir' -o -name '#recycle' -o -name '#snapshot' -o -name '.TemporaryItems' \) -prune -o \
    -type d -exec sh -c '
        tool=$1
        user=$2
        shift 2
        for path do
            while :; do
                current=$("$tool" -get "$path" 2>/dev/null || true)
                index=$(printf "%s\n" "$current" | sed -n "/user:$user:.*(level:0)/s/^[[:space:]]*\[\([0-9][0-9]*\)\].*/\1/p" | head -n 1)
                [ -n "$index" ] || break
                "$tool" -del "$path" "$index" >/dev/null || exit 1
            done
            "$tool" -add "$path" "user:$user:allow:r-x----------:----" >/dev/null || exit 1
        done
    ' sh "$SYNOACLTOOL" "$RUNNER_USER" {} + \
    || fail "Could not configure read-only access to media directories."

# Grant read-only access to actual media files, excluding Synology metadata.
find "$MEDIA_DIR" \
    \( -name '@eaDir' -o -name '#recycle' -o -name '#snapshot' -o -name '.TemporaryItems' \) -prune -o \
    -type f -exec sh -c '
        tool=$1
        user=$2
        shift 2
        for path do
            while :; do
                current=$("$tool" -get "$path" 2>/dev/null || true)
                index=$(printf "%s\n" "$current" | sed -n "/user:$user:.*(level:0)/s/^[[:space:]]*\[\([0-9][0-9]*\)\].*/\1/p" | head -n 1)
                [ -n "$index" ] || break
                "$tool" -del "$path" "$index" >/dev/null || exit 1
            done
            "$tool" -add "$path" "user:$user:allow:r-----------:----" >/dev/null || exit 1
        done
    ' sh "$SYNOACLTOOL" "$RUNNER_USER" {} + \
    || fail "Could not configure read-only access to media files."

# The local VLC password is not part of the runner's required inputs.
if [ -f "$VLC_ENV" ]; then
    ensure_acl "$VLC_ENV" "user:$RUNNER_USER:deny:r-----------:----"
fi

mkdir -p "$RUNNER_DIR"
chown -R "$RUNNER_USER" "$RUNNER_DIR"
chmod 700 "$RUNNER_DIR"

run_as_runner() {
    su -s /bin/sh -c "cd / && $1" "$RUNNER_USER"
}

# Parent folders may be listable under the NAS's existing everyone ACL. The
# runner only needs traversal; this check does not claim to hide their names.
for start in "$SCRIPT_DIR" "$MEDIA_DIR"; do
    path=$(dirname "$start")
    while [ "$path" != "/" ]; do
        run_as_runner "test -x '$path'" || fail "Runner account cannot traverse $path."
        path=$(dirname "$path")
    done
done

run_as_runner "test -r '$SCRIPT_DIR/install-runner.sh' && test -x '$SCRIPT_DIR' && test -w '$RUNNER_DIR'" \
    || fail "Runner account cannot read the installer or write its private runner directory."
run_as_runner "test -r '$MEDIA_DIR' && test -x '$MEDIA_DIR' && ! test -w '$MEDIA_DIR' && ls -A '$MEDIA_DIR' >/dev/null" \
    || fail "Runner account cannot list/read the media directory or can write to it."

run_as_runner "find '$MEDIA_DIR' \\( -name '@eaDir' -o -name '#recycle' -o -name '#snapshot' -o -name '.TemporaryItems' \\) -prune -o -type d -exec sh -c 'for path do test -x \"\$path\" && test ! -w \"\$path\" || exit 1; done' sh {} +" \
    || fail "Runner account cannot traverse or can write to one or more media directories."
run_as_runner "find '$MEDIA_DIR' \\( -name '@eaDir' -o -name '#recycle' -o -name '#snapshot' -o -name '.TemporaryItems' \\) -prune -o -type f -exec sh -c 'for path do test -r \"\$path\" && test ! -w \"\$path\" || exit 1; done' sh {} +" \
    || fail "Runner account cannot read every media file or can write to one or more media files."

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
