#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNNER_DIR="${SCRIPT_DIR}/actions-runner"

if [ ! -f "${RUNNER_DIR}/.runner" ]; then
    printf '%s\n' "GitHub Actions runner is not configured. Run install-runner.sh first." >&2
    exit 1
fi

cd "$RUNNER_DIR"
exec ./run.sh
