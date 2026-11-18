# Synology remote VLC runner

This directory installs the **official GitHub Actions self-hosted runner** directly on a Synology DSM x86_64 host. It is deliberately not a Docker container: the runner can reach the test VLC endpoint at `127.0.0.1:18080` and read the fixture directory using the host filesystem.

## Install

Run these steps as a dedicated, non-administrator DSM account. Do not run the runner as root.

1. In GitHub, open **Settings → Actions → Runners → New self-hosted runner**, select **Linux / x64**, and generate a registration token.
2. From the checked-out repository on the NAS, run:

   ```sh
   chmod +x ctrlvee-vlc-test/runner/*.sh
   ./ctrlvee-vlc-test/runner/install-runner.sh
   ```

3. Paste the short-lived registration token when prompted. The installer downloads the official runner v2.338.2 release, verifies its pinned SHA-256 checksum, and configures the runner with the `vlc-lan` label. The token is not written to disk or shell history.
4. Start the runner:

   ```sh
   ./ctrlvee-vlc-test/runner/run-runner.sh
   ```

The runner receives GitHub's default `self-hosted`, `linux`, and `x64` labels in addition to `vlc-lan`. It has no Docker socket, no Docker-management privileges, and no access to production media credentials.

## Start at boot

In DSM **Control Panel → Task Scheduler**:

1. Create **Triggered Task → User-defined script**.
2. Set the user to the same dedicated runner account.
3. Trigger on **Boot-up**.
4. Set the user-defined script to the absolute path of `ctrlvee-vlc-test/runner/run-runner.sh` in the NAS checkout.
5. Save and run the task once to verify it starts.

Keep the runner process running in the foreground under Task Scheduler. Do not configure a second copy of the same runner.

## Requirements and security

- Synology DSM on x86_64 (the workflow also requires the `x64` label).
- Outbound HTTPS access to GitHub.
- `curl`, `tar`, and `sha256sum`.
- The runner account can read/traverse `/volume1/docker/ctrlvee-vlc-test/media`.
- The dedicated test VLC container remains bound to host loopback at `127.0.0.1:18080`.
- Restrict write access to the repository's `main` branch. This runner must only execute the manually dispatched trusted-main workflow, never pull-request or fork code.

Runner files and credentials are stored under `actions-runner/`, which is ignored by Git. Never commit that directory, registration tokens, or runner credentials.

To remove the runner, stop its DSM task and process, then use GitHub **Settings → Actions → Runners** to remove the offline runner entry. After that, remove the local `actions-runner/` directory.
