# Private Synology VLC integration environment

This opt-in deployment runs a dedicated VLC test service and a repository-scoped GitHub Actions runner on the Synology. The runner runs inside Debian 12 rather than on DSM itself: current GitHub Actions JavaScript actions use Node 24, whose glibc requirements are newer than DSM 7.1.1 provides. The container supplies a compatible userspace without changing DSM system libraries.

The VLC HTTP interface is published only on the Synology loopback address (127.0.0.1:18080). The runner uses host networking to reach it. Do not add a reverse-proxy route, router port-forward, or public DNS record for this port.

## 1. Confirm the Synology architecture

Connect to DSM over SSH and run:

    uname -m

This deployment targets x86_64. If the result is different, stop and adjust the runner image and workflow labels before proceeding.

## 2. Prepare the isolated media directory

Create the dedicated host directory:

    mkdir -p /volume1/docker/ctrlvee-vlc-test/media

Place one or more representative audio/video files in it. The remote integration test discovers supported files and plays them through VLC. The test does not create, edit, rename, or delete media files; it only adds and removes its own VLC playlist entries.

Do not point this directory at the production media library.

## 3. Deploy the dedicated VLC test service

Clone the repository to a stable Synology path, for example `/volume1/docker/ctrlvee`, and check out the reviewed `main` branch. From the repository:

    cd /volume1/docker/ctrlvee/ctrlvee-vlc-test
    cp .env.example .env
    # Edit .env and set a unique VLC_TEST_PASSWORD.
    chmod 600 .env
    docker-compose config --quiet
    docker-compose up -d --build vlc-test

Use `docker compose` instead of `docker-compose` if Compose V2 is installed.

The VLC container:
- runs VLC as an unprivileged user;
- has a read-only root filesystem and drops Linux capabilities;
- mounts only `/volume1/docker/ctrlvee-vlc-test/media` at `/media`, read-only;
- publishes its HTTP interface only as `127.0.0.1:18080` on the Synology host.

Verify it locally:

    set -a
    . ./.env
    set +a
    curl --fail --silent --show-error \
      --user ":$VLC_TEST_PASSWORD" \
      http://127.0.0.1:18080/requests/status.xml

The response should be VLC status XML.

## 4. Build, configure, and start the Debian Actions runner

From the repository root, run:

    cd /volume1/docker/ctrlvee
    sh ctrlvee-vlc-test/runner/setup-container-runner.sh

The script:
1. Builds the official GitHub Actions runner v2.338.2 into a Debian 12 image and verifies its pinned SHA-256 checksum.
2. Prompts for a short-lived repository registration token without echoing it.
3. Pipes the token to a one-shot configuration container over stdin (not a command-line argument or environment variable).
4. Stores the runner installation, credentials, and work directory in the persistent Docker volume `ctrlvee-actions-runner-data`.
5. Starts the runner container.

The runner registers as `synology-vlc-lan-container` with labels `self-hosted`, `linux`, `x64`, and `vlc-lan`. It uses host networking so the workflow's `127.0.0.1:18080` address reaches the Synology host.

The runner image defaults to UID 1058 and GID 100 to match the dedicated DSM account created by the previous setup. If those numeric IDs differ on your NAS, override the `RUNNER_UID` and `RUNNER_GID` Docker build arguments before building.

The runner container:
- runs as an unprivileged Linux user using the existing DSM runner identity (UID 1058, GID 100 by default), preserving the media ACLs granted to `ctrlvee-runner`;
- has a read-only root filesystem;
- has no Docker socket, Docker-management permissions, host root mount, or repository checkout mount;
- mounts the dedicated media directory read-only at its existing absolute path;
- does not receive `VLC_TEST_PASSWORD` or read the local `.env` file.

The Compose project reads `.env` for interpolation of the VLC service, but the password is not included in the runner service's environment.

## 5. Migrate away from the old native DSM runner

If the old native `synology-vlc-lan` runner is still registered, leave it alone until the new container runner is shown as **Idle/online** in GitHub **Settings → Actions → Runners**.

After the container runner is confirmed online:
1. Stop the old `ctrlvee-runner` process.
2. Disable its DSM Task Scheduler boot task.
3. Remove the old `synology-vlc-lan` registration in GitHub Settings → Actions → Runners.
4. Optionally remove the old DSM account and old `actions-runner/` files yourself after confirming you no longer need them.

The setup script does not modify DSM accounts, ACLs, scheduled tasks, or the old runner registration.

## 6. Configure GitHub Actions

Add repository secret `REMOTE_VLC_PASSWORD` with the same value as the local `VLC_TEST_PASSWORD`. The manually dispatched workflow targets `self-hosted`, `linux`, `x64`, and `vlc-lan`, and explicitly checks out trusted `main`.

Keep write access to `main` restricted. Do not add this runner to pull-request or fork workflows.

## 7. Run and manage the remote integration test

Open **Actions → Remote VLC integration → Run workflow** and dispatch it from `main`.

The workflow verifies the VLC endpoint and media directory, installs Python test dependencies, then tests real playback, pause, resume, seek (for files at least 35 seconds long), and stop for every supported media file it discovers. It removes only playlist entries created by the test. It never clears the playlist or changes media file bytes.

Manage the runner from `ctrlvee-vlc-test`:

| Action | Command |
| --- | --- |
| Status | `docker-compose ps actions-runner` |
| Logs | `docker-compose logs -f actions-runner` |
| Restart | `docker-compose restart actions-runner` |
| Stop | `docker-compose stop actions-runner` |
| Start | `docker-compose up -d actions-runner` |
| Rebuild | `docker-compose build --pull actions-runner && docker-compose up -d actions-runner` |

The runner configuration persists in `ctrlvee-actions-runner-data`. Do not delete this volume for routine image updates. To retire the runner, stop it, remove its GitHub registration, then remove the volume only if you intend to delete its runner credentials and job workspace.
