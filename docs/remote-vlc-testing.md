# Private Synology VLC integration environment

This deployment adds a **dedicated test VLC instance** and a **repository-scoped GitHub Actions runner** on the Synology. It must not point at the production Plex/VLC endpoint. The Compose file uses format 3.7 and supports both legacy `docker-compose` (Compose V1) and `docker compose` (Compose V2); the top-level `name` property is deliberately omitted because Compose V1 rejects it.

The VLC HTTP interface is published only on the Synology loopback address (127.0.0.1:18080). The native runner reaches it locally. Do not add a reverse-proxy route, router port-forward, or public DNS record for this port.

## 1. Confirm the Synology architecture

Connect to DSM over SSH from your LAN or through your private remote-access method, then run:

    uname -m

This first-run guide assumes x86_64. If it reports aarch64 or another architecture, stop before downloading the runner and adjust the workflow runner label and runner package to match.

Do not expose SSH to the public internet. QuickConnect can remain enabled for DSM. Tailscale is recommended for private SSH/admin access while away from home; it does not need any inbound router ports.

## 2. Create the isolated test media directory

Create the fixed host directory used by both Compose and the runner:

    mkdir -p /volume1/docker/ctrlvee-vlc-test/media

Place one or more representative audio/video files in this directory (subdirectories are supported). The integration test discovers all supported files automatically. The runner account needs read/traverse access; it does not need write access. Files and directories must also be readable/traversable by the VLC container's unprivileged UID 10001. The VLC container mounts this directory read-only.

Do not point this directory at the production media library.

## 3. Deploy the test VLC container

Clone the repository to a stable Synology path, for example /volume1/docker/ctrlvee, and check out the reviewed main branch. The self-contained Compose deployment is in ctrlvee-vlc-test/. The Compose file contains the image, ports, media mount, and hardening configuration. Only the VLC password is supplied through the local, uncommitted .env file:

    cd /volume1/docker/ctrlvee/ctrlvee-vlc-test
    cp .env.example .env
    # Edit .env and set a unique VLC_TEST_PASSWORD.
    chmod 600 .env
    docker-compose config --quiet
    docker-compose up -d --build

The container:
- runs VLC as an unprivileged user;
- has a read-only root filesystem and drops Linux capabilities;
- mounts only /volume1/docker/ctrlvee-vlc-test/media at /media, read-only;
- has its HTTP interface bound to container port 8080;
- publishes that port only as 127.0.0.1:18080 on the Synology host.

Check it locally on the Synology (the source command loads VLC_TEST_PASSWORD from the local .env into your shell):

    set -a
    . ./.env
    set +a
    curl --fail --silent --show-error \
      --user ":$VLC_TEST_PASSWORD" \
      http://127.0.0.1:18080/requests/status.xml

The response should be VLC status XML. Do not test by opening port 18080 from the internet; it is intentionally loopback-only.

## 4. Install the self-hosted runner on the Synology

The repository includes an account bootstrapper, runner installer, and startup script under `ctrlvee-vlc-test/runner/`. From the repository checkout, run this from an interactive SSH session:

    sudo sh ctrlvee-vlc-test/runner/setup-runner.sh

The bootstrapper uses DSM's `synouser` to create the dedicated non-administrator account `ctrlvee-runner` with a generated, unguessable password that is never displayed or saved. It uses `synoacltool` to remove stale explicit ACL entries for that account, restore traversal-only access on each parent directory, grant read/traverse access to the runner scripts, and grant read-only access to actual media directories and files. It deliberately avoids DENY entries on parent directories because Synology ACL inheritance can carry those entries into the media subtree and block fixture access. The runner's ability to list parent-directory names therefore follows the NAS's existing ACLs; the setup does not claim to hide those names. It checks that the runner can read fixtures but cannot write media directories or files, read the local VLC `.env` password file, or access the Docker socket when present. Synology `@eaDir`, recycle-bin, snapshot, and temporary metadata trees are excluded from fixture permissions and checks. It then drops privileges and runs the installer as `ctrlvee-runner`.

The installer downloads the official GitHub Actions runner v2.338.2 release for Linux x64, verifies its pinned SHA-256 checksum, and prompts for the short-lived registration token. It configures the runner as `synology-vlc-lan` with the additional `vlc-lan` label. GitHub supplies the standard `self-hosted`, `linux`, and `x64` labels automatically. The token is not saved.

For persistence across DSM restarts, create a DSM **Task Scheduler → Triggered Task → User-defined script** task running as `ctrlvee-runner`, triggered at boot, with the absolute path to `ctrlvee-vlc-test/runner/run-runner.sh`. Keep the runner in the foreground, ensure only one runner process is started, and do not expose the runner listener or install it on a public-facing host.

The runner is installed under `ctrlvee-vlc-test/runner/actions-runner/`, which is ignored by Git. It does not mount or access the Docker socket.

The runner is persistent and can execute repository code from the trusted main branch. Keep write access to main restricted and do not add this runner to pull-request workflows or workflows that execute fork/untrusted code.

## 5. Configure GitHub Actions

Add repository secret **REMOTE_VLC_PASSWORD** with the same value as the local **VLC_TEST_PASSWORD**. No GitHub repository variable is needed for the media path: the runner and Compose deployment both use the fixed directory /volume1/docker/ctrlvee-vlc-test/media.

The workflow assumes:
- runner labels self-hosted, linux, x64, and vlc-lan;
- test VLC at 127.0.0.1:18080;
- the runner can read /volume1/docker/ctrlvee-vlc-test/media, which the container mounts at /media.

## 6. Run the remote integration test

After the runner is online and media files are present, go to **Actions → Remote VLC integration → Run workflow** and dispatch it from main.

The workflow is manually dispatched only, explicitly checks out main, and targets only the vlc-lan runner label. It does not run for pull requests, pushes, or fork code.

The remote test:
1. verifies status and playlist access;
2. discovers all supported audio/video files in the dedicated media directory (including subdirectories);
3. asks VLC to enqueue and play each selected file;
4. verifies pause, resume, and stop for every selected file, and seek when the file is at least 35 seconds long;
5. removes only the playlist items created by that test.

It never writes to, renames, or deletes the media files. It never clears the playlist or removes pre-existing playlist entries. Keep this VLC instance dedicated to CtrlVee integration tests; do not use it for normal playback.

## 7. Remote administration

Keep QuickConnect and existing reverse-proxy services as they are. For private administration from outside the home, install Tailscale on the Synology using the official Synology package/instructions and on the administrator's device. Use the Tailscale address for SSH/DSM administration if desired. Do not publish VLC's HTTP port through Tailscale Funnel, a reverse proxy, or router port forwarding.

## Troubleshooting

- Runner offline: check the DSM boot task, runner process, outbound DNS/HTTPS, and GitHub runner status.
- VLC status unavailable: check docker-compose logs vlc-test, the container status, and the local curl command.
- Authentication failure: ensure REMOTE_VLC_PASSWORD and local VLC_TEST_PASSWORD match.
- No media fixtures found: confirm the directory contains at least one non-empty supported media file and that the runner account can read it.
- Workflow says no runner: confirm all four labels, especially vlc-lan, and that the runner is online.
