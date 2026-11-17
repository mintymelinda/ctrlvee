# Private Synology VLC integration environment

This deployment adds a **dedicated test VLC instance** and a **repository-scoped GitHub Actions runner** on the Synology. It must not point at the production Plex/VLC endpoint.

The VLC HTTP interface is published only on the Synology loopback address (127.0.0.1:18080). The native runner reaches it locally. Do not add a reverse-proxy route, router port-forward, or public DNS record for this port.

## 1. Confirm the Synology architecture

Connect to DSM over SSH from your LAN or through your private remote-access method, then run:

    uname -m

This first-run guide assumes x86_64. If it reports aarch64 or another architecture, stop before downloading the runner and adjust the workflow runner label and runner package to match.

Do not expose SSH to the public internet. QuickConnect can remain enabled for DSM. Tailscale is recommended for private SSH/admin access while away from home; it does not need any inbound router ports.

## 2. Create the isolated test media directory

Choose a persistent volume path. The examples use:

    /volume1/docker/ctrlvee-vlc-test/media

Create it in DSM File Station or over SSH. Give the dedicated GitHub runner account write access and allow the VLC container to read files. Do not point this directory at the production media library.

## 3. Deploy the test VLC container

Clone the repository to a stable Synology path, for example /volume1/docker/ctrlvee, and check out the reviewed main branch. In infra/remote-vlc, create a local .env file (never commit it):

    VLC_TEST_PASSWORD=replace-with-a-long-random-password
    VLC_TEST_MEDIA_DIR=/volume1/docker/ctrlvee-vlc-test/media

Restrict the file permissions so only the deployment administrator can read it. Then run:

    cd /volume1/docker/ctrlvee/ctrlvee-vlc-test
    cp .env.example .env
    # Edit .env and set a unique VLC_TEST_PASSWORD and the media directory.
    chmod 600 .env
    docker compose -f docker-compose.yml config --quiet
    docker compose -f docker-compose.yml up -d --build

The container:
- runs VLC as an unprivileged user;
- has a read-only root filesystem and drops Linux capabilities;
- mounts only the dedicated test media directory, read-only;
- has its HTTP interface bound to container port 8080;
- publishes that port only as 127.0.0.1:18080 on the Synology host.

Check it locally on the Synology:

    curl --fail --silent --show-error \
      --user ":$VLC_TEST_PASSWORD" \
      http://127.0.0.1:18080/requests/status.xml

The response should be VLC status XML. Do not test by opening port 18080 from the internet; it is intentionally loopback-only.

## 4. Install the self-hosted runner on the Synology

Use a dedicated, non-administrator DSM account for the runner. It needs:
- access to the repository working directory;
- write access to the dedicated test media directory;
- no access to production media or production VLC credentials;
- no Docker socket or general container-management privileges.

In GitHub, open **Settings → Actions → Runners → New self-hosted runner**, select **Linux / x64**, and follow GitHub's current download and extraction commands. GitHub generates a short-lived registration token; do not store it in the repository or a committed file.

From the extracted runner directory, configure it with the generated token:

    ./config.sh \
      --url https://github.com/mintymelinda/ctrlvee \
      --token <SHORT_LIVED_REGISTRATION_TOKEN> \
      --name synology-vlc-lan \
      --labels vlc-lan \
      --work _work \
      --unattended

The standard self-hosted, Linux, and X64 labels are added automatically; vlc-lan is the additional routing label used by the workflow.

Run the runner as the dedicated account, not root:

    ./run.sh

For persistence across DSM restarts, create a DSM **Task Scheduler → Triggered Task → User-defined script** task that starts the runner at boot as that same account. Set the working directory to the runner installation directory before invoking ./run.sh. Ensure only one runner process is started. Do not expose the runner listener or install it on a public-facing host.

The runner is persistent and can execute repository code from the trusted main branch. Keep write access to main restricted and do not add this runner to pull-request workflows or workflows that execute fork/untrusted code.

## 5. Configure GitHub Actions

In **Settings → Secrets and variables → Actions**, add:

- Repository secret REMOTE_VLC_PASSWORD: the same password as VLC_TEST_PASSWORD.
- Repository variable REMOTE_VLC_MEDIA_DIR: the exact host path mounted into the container, e.g. /volume1/docker/ctrlvee-vlc-test/media.

The workflow assumes:
- runner labels self-hosted, linux, x64, and vlc-lan;
- test VLC at 127.0.0.1:18080;
- the runner and VLC container share the same host media directory, with the container path /media.

## 6. Run the remote integration test

After the setup PR is merged and the runner is online, go to **Actions → Remote VLC integration → Run workflow** and dispatch it from main.

The workflow is manually dispatched only, explicitly checks out main, and targets only the vlc-lan runner label. It does not run for pull requests, pushes, or fork code.

The remote test:
1. verifies status and playlist access;
2. creates a uniquely named WAV fixture in the dedicated host media directory;
3. asks VLC to enqueue and play that fixture;
4. verifies pause, resume, seek, and stop against the test item's playlist ID;
5. removes only the playlist item created by that test and deletes its fixture.

It never clears the playlist and never removes pre-existing playlist entries. Keep this VLC instance dedicated to CtrlVee integration tests anyway; do not use it for normal playback.

## 7. Remote administration

Keep QuickConnect and existing reverse-proxy services as they are. For private administration from outside the home, install Tailscale on the Synology using the official Synology package/instructions and on the administrator's device. Use the Tailscale address for SSH/DSM administration if desired. Do not publish VLC's HTTP port through Tailscale Funnel, a reverse proxy, or router port forwarding.

## Troubleshooting

- Runner offline: check the DSM boot task, runner process, outbound DNS/HTTPS, and GitHub runner status.
- VLC status unavailable: check docker compose logs vlc-test, the container status, and the local curl command.
- Authentication failure: ensure REMOTE_VLC_PASSWORD and local VLC_TEST_PASSWORD match.
- Fixture not found: verify the runner can write the host media directory and the container mounts that exact directory at /media.
- Workflow says no runner: confirm all four labels, especially vlc-lan, and that the runner is online.
