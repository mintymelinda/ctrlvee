# Synology remote VLC Actions runner

The supported runner runs in a Debian 12 (Bookworm) container, not directly on DSM. GitHub Actions now executes JavaScript actions on Node 24, which requires a newer glibc than the one shipped with DSM 7.1.1. Debian 12 supplies the required runtime without replacing or modifying DSM system libraries.

The runner container uses the existing DSM runner identity (UID 1058, GID 100 by default) so the Synology ACLs already granted to `ctrlvee-runner` continue to apply to the media mount. The Dockerfile exposes `RUNNER_UID` and `RUNNER_GID` build arguments if that DSM account has different numeric IDs. The runner container uses host networking so it can reach the dedicated VLC test service at `127.0.0.1:18080`. It mounts the test media directory read-only at the same absolute host path used by the workflow. It has no Docker socket, no Docker-management permissions, and receives no VLC password.

## Prerequisites

- Synology DSM x86_64 with Container Manager / Docker Engine.
- Docker Compose V2 (`docker compose`) or legacy Compose V1 (`docker-compose`).
- The dedicated VLC test service deployed from `ctrlvee-vlc-test/docker-compose.yml`.
- `ctrlvee-vlc-test/.env` created from `.env.example`, containing only `VLC_TEST_PASSWORD`.
- `ctrlvee-vlc-test/media/` exists and contains at least one supported media file.
- Outbound HTTPS access to GitHub.

## Configure and start the container runner

From an interactive SSH session:

```sh
cd /volume1/docker/ctrlvee
sh ctrlvee-vlc-test/runner/setup-container-runner.sh
```

The script builds the Debian runner image, prompts for a short-lived GitHub repository runner registration token without echoing it, pipes the token to the one-shot configuration container over stdin, and starts the persistent runner service. The token is not stored in a file or container environment.

The persistent runner configuration and work directory live in a Docker named volume, `ctrlvee-actions-runner-data`. The container is named `ctrlvee-actions-runner` and registers as `synology-vlc-lan-container` with the additional `vlc-lan` label. GitHub supplies the standard `self-hosted`, `linux`, and `x64` labels.

Verify the new runner is online and Idle under GitHub **Settings → Actions → Runners** before removing the old native runner.

## Migrate from the previous native DSM runner

The previous host-installed runner cannot execute Node 24 on DSM 7.1.1. The container setup intentionally registers a second runner so the existing working registration is left untouched during migration.

After the new `synology-vlc-lan-container` runner is online:

1. Stop the old `ctrlvee-runner` process and disable its DSM boot task.
2. In GitHub **Settings → Actions → Runners**, remove the old offline `synology-vlc-lan` runner.
3. The old DSM account and files can then be removed manually if you no longer need them. This setup does not change DSM users, ACLs, or existing runner files.

Do not remove the old runner until the container runner is confirmed online.

## Day-to-day management

Run from `/volume1/docker/ctrlvee/ctrlvee-vlc-test`:

| Action | Command |
| --- | --- |
| Status | `docker compose ps actions-runner` |
| Logs | `docker compose logs -f actions-runner` |
| Restart | `docker compose restart actions-runner` |
| Stop | `docker compose stop actions-runner` |
| Start | `docker compose up -d actions-runner` |
| Update image | `docker compose build --pull actions-runner && docker compose up -d actions-runner` |

Use `docker-compose` instead of `docker compose` if the NAS only has Compose V1.

To remove the container runner, stop the service, remove the GitHub runner registration from GitHub Settings, then remove the named volume `ctrlvee-actions-runner-data` only if you intentionally want to delete its configuration and job workspace. Do not remove the volume as part of a routine image rebuild.

## Isolation and network model

- The runner uses Docker host networking to access the VLC HTTP API on host loopback. This is required because a container bridge's `127.0.0.1` would refer to that container, not the NAS.
- The runner sees `/volume1/docker/ctrlvee-vlc-test/media` read-only at the same path expected by the workflow. The VLC container separately mounts the same host directory read-only at `/media`.
- The runner has a persistent named volume for its own installation, credentials, and job workspace.
- The runner container does not mount `/var/run/docker.sock`, the repository checkout, the host root filesystem, or the VLC `.env` file.
- The Compose service does not pass `VLC_TEST_PASSWORD` into the runner container. The GitHub workflow receives it only as the `REMOTE_VLC_PASSWORD` secret for the integration test process.
- The remote VLC HTTP endpoint remains bound to `127.0.0.1:18080`. Do not expose it through a reverse proxy, router port forwarding, or public tunnel.

The runner is a trusted repository-level execution environment. Keep write access to `main` restricted and do not target it from pull-request or fork workflows.
