#!/bin/bash
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
    chown runner:runner /runner
    exec gosu runner:runner "$0" "$@"
fi

cd /runner

if [[ "${RUNNER_MODE:-run}" == "configure" ]]; then
    if [[ -f .runner ]]; then
        echo "Runner is already configured in the persistent runner volume." >&2
        exit 1
    fi
    if ! IFS= read -r RUNNER_TOKEN; then
        echo "A one-time GitHub registration token must be supplied on stdin." >&2
        exit 1
    fi
    if [[ -z "$RUNNER_TOKEN" ]]; then
        echo "The GitHub registration token cannot be empty." >&2
        exit 1
    fi

    ./config.sh \
        --url "https://github.com/mintymelinda/ctrlvee" \
        --token "$RUNNER_TOKEN" \
        --name "synology-vlc-lan-container" \
        --labels "vlc-lan" \
        --work "_work" \
        --unattended
    unset RUNNER_TOKEN
    echo "Container runner configured. Start it with: docker compose up -d actions-runner"
    exit 0
fi

if [[ ! -f .runner ]]; then
    echo "Runner is not configured. Run ctrlvee-vlc-test/runner/setup-container-runner.sh first." >&2
    exit 1
fi

exec ./run.sh
