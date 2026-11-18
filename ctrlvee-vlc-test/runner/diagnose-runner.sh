#!/bin/sh
# Read-only diagnostic for the Synology-hosted GitHub Actions runner.
# Run as ctrlvee-runner: sh diagnose-runner.sh
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)
RUNNER_DIR="${SCRIPT_DIR}/actions-runner"
# Prefer the runner-local DSM compatibility helper when no system ldd exists.
if ! command -v ldd >/dev/null 2>&1 && [ -x "$RUNNER_DIR/.local-tools/ldd" ]; then
    PATH="$RUNNER_DIR/.local-tools:$PATH"
    export PATH
fi
FAILURES=0
WARNINGS=0

section() {
    printf '\n=== %s ===\n' "$1"
}

ok() {
    printf '[ OK ] %s\n' "$1"
}

warn() {
    WARNINGS=$((WARNINGS + 1))
    printf '[WARN] %s\n' "$1"
}

fail() {
    FAILURES=$((FAILURES + 1))
    printf '[FAIL] %s\n' "$1"
}

check_command() {
    name=$1
    required=$2
    path=$(command -v "$name" 2>/dev/null || true)
    if [ -n "$path" ]; then
        ok "$name: $path"
    elif [ "$required" = required ]; then
        fail "$name: not found in PATH"
    else
        warn "$name: not found in PATH (optional for this stage)"
    fi
}

section "Host and account"
printf 'Date: '
date 2>/dev/null || printf 'unavailable\n'
printf 'Hostname: '
hostname 2>/dev/null || printf 'unavailable\n'
printf 'User: '
id 2>&1
printf 'Architecture: '
uname -m 2>&1
printf 'Kernel: '
uname -sr 2>&1

if [ "$(id -u)" -eq 0 ]; then
    fail "Running as root; install and run the Actions runner as ctrlvee-runner."
else
    ok "Running as a non-root account."
fi
if id 2>/dev/null | grep -q 'ctrlvee-runner'; then
    ok "Current account is ctrlvee-runner."
else
    warn "Current account is not named ctrlvee-runner."
fi
if [ "$(uname -m 2>/dev/null)" = x86_64 ]; then
    ok "Host architecture matches the configured Linux x64 runner."
else
    fail "Host architecture is not x86_64."
fi

for version_file in /etc.defaults/VERSION /etc/VERSION; do
    if [ -r "$version_file" ]; then
        printf 'DSM version source (%s):\n' "$version_file"
        sed -n '1,8p' "$version_file"
        break
    fi
done

section "System commands"
# Required by the bootstrapper, installer, runner configuration and diagnostics.
for command_name in sh bash curl tar gzip sha256sum stty git awk sed grep find id uname mktemp mkdir cat chmod; do
    check_command "$command_name" required
done
if command -v ldd >/dev/null 2>&1; then
    ok "ldd: $(command -v ldd)"
elif [ -x /lib/ld-linux-x86-64.so.2 ] || [ -x /lib64/ld-linux-x86-64.so.2 ] || [ -x /usr/lib/ld-linux-x86-64.so.2 ]; then
    warn "System ldd is absent; the installer can provide a runner-local compatibility helper using the available glibc dynamic loader."
else
    fail "Neither ldd nor a compatible x86_64 glibc dynamic loader was found."
fi
# Used by setup-runner.sh and helpful for host inspection.
for command_name in su chown openssl synouser synoacltool readelf getconf; do
    check_command "$command_name" optional
done

section "C library and runtime"
if command -v getconf >/dev/null 2>&1; then
    libc_version=$(getconf GNU_LIBC_VERSION 2>&1 || true)
    case "$libc_version" in
        *glibc*) ok "C library: $libc_version" ;;
        *) warn "Could not identify glibc via getconf: $libc_version" ;;
    esac
fi
if command -v ldd >/dev/null 2>&1; then
    ldd_version=$(ldd --version 2>&1 | sed -n '1p')
    printf 'ldd version: %s\n' "$ldd_version"
elif [ -x /lib/ld-linux-x86-64.so.2 ] || [ -x /lib64/ld-linux-x86-64.so.2 ] || [ -x /usr/lib/ld-linux-x86-64.so.2 ]; then
    warn "System ldd is absent. The installer will provision a private compatibility helper inside actions-runner."
else
    fail "ldd is missing and no compatible x86_64 glibc dynamic loader was found."
fi

section "Runner files and permissions"
printf 'Expected runner directory: %s\n' "$RUNNER_DIR"
if [ -d "$RUNNER_DIR" ]; then
    ok "Runner directory exists."
    [ -r "$RUNNER_DIR" ] && ok "Runner directory is readable." || fail "Runner directory is not readable."
    [ -w "$RUNNER_DIR" ] && ok "Runner directory is writable by this account." || fail "Runner directory is not writable by this account."
else
    warn "Runner directory does not exist yet; the archive may not have been extracted."
fi

for relative_path in config.sh run.sh bin/Runner.Listener; do
    file_path="$RUNNER_DIR/$relative_path"
    if [ -f "$file_path" ]; then
        [ -r "$file_path" ] && ok "$relative_path exists and is readable." || fail "$relative_path exists but is not readable."
        case "$relative_path" in
            *.sh|bin/Runner.Listener)
                [ -x "$file_path" ] && ok "$relative_path is executable." || fail "$relative_path is not executable."
                ;;
        esac
    else
        warn "$relative_path is absent (runner download/extraction may be incomplete)."
    fi
done

if [ -f "$RUNNER_DIR/.runner" ]; then
    ok "Runner configuration marker .runner exists."
else
    warn "Runner is not configured yet (.runner is absent)."
fi

section "Runner shared-library dependencies"
if [ -x "$RUNNER_DIR/bin/Runner.Listener" ]; then
    if command -v ldd >/dev/null 2>&1; then
        for binary in "$RUNNER_DIR/bin/Runner.Listener" "$RUNNER_DIR/bin/Runner.Worker" "$RUNNER_DIR/bin/Runner.PluginHost" "$RUNNER_DIR"/externals/node*/bin/node; do
            [ -x "$binary" ] || continue
            printf '\n-- %s --\n' "$binary"
            dependency_output=$(ldd "$binary" 2>&1 || true)
            printf '%s\n' "$dependency_output"
            if printf '%s\n' "$dependency_output" | grep -q 'not found'; then
                fail "One or more shared libraries are missing for $binary."
            elif printf '%s\n' "$dependency_output" | grep -q 'not a dynamic executable'; then
                warn "$binary is not a dynamically linked ELF executable; skipped."
            else
                ok "No unresolved shared libraries reported for $binary."
            fi
        done
    else
        warn "Skipped shared-library inspection because ldd is unavailable."
    fi
else
    warn "Runner.Listener is not present; shared-library inspection cannot run."
fi

section "Common Linux runtime libraries"
# These names cover the common native dependencies used by the Actions runner
# and its bundled Node runtimes. ldd output above is authoritative when present.
if command -v ldd >/dev/null 2>&1 && [ -x "$RUNNER_DIR/bin/Runner.Listener" ]; then
    all_dependencies=$(ldd "$RUNNER_DIR/bin/Runner.Listener" 2>&1 || true)
    for library in libicu libssl libcrypto libkrb5 libcurl libunwind libuuid libstdc++ libgcc_s libz.so; do
        if printf '%s\n' "$all_dependencies" | grep -q "$library"; then
            ok "$library appears in Runner.Listener dependency output."
        else
            warn "$library was not listed for Runner.Listener (it may be optional or loaded by another component)."
        fi
    done
else
    warn "Cannot inspect runtime library names until ldd and Runner.Listener are available."
fi

section "Outbound connectivity"
if command -v curl >/dev/null 2>&1; then
    if curl --fail --silent --show-error --location --connect-timeout 5 --max-time 15 \
        --output /dev/null https://github.com/actions/runner/releases/latest; then
        ok "HTTPS access to github.com works."
    else
        fail "Could not reach https://github.com/actions/runner/releases/latest over HTTPS."
    fi
else
    fail "Cannot test GitHub connectivity because curl is missing."
fi

section "Security-sensitive paths (read-only checks)"
for protected_path in "$(dirname "$SCRIPT_DIR")/.env" /var/run/docker.sock; do
    if [ -e "$protected_path" ]; then
        if [ -r "$protected_path" ] || [ -w "$protected_path" ]; then
            fail "Runner account has read or write access to $protected_path."
        else
            ok "Runner account cannot read or write $protected_path."
        fi
    else
        ok "$protected_path is absent."
    fi
done

section "Summary"
printf 'Failures: %s\nWarnings: %s\n' "$FAILURES" "$WARNINGS"
if [ "$FAILURES" -gt 0 ]; then
    printf '%s\n' "One or more blockers were detected. Share this complete output for diagnosis."
    exit 1
fi
printf '%s\n' "No hard blockers detected by the checks above."
exit 0
