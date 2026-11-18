# Synology remote VLC runner

This directory installs the **official GitHub Actions self-hosted runner** directly on a Synology DSM x86_64 host. It is deliberately not a Docker container: the runner can reach the test VLC endpoint at `127.0.0.1:18080` and read the fixture directory using the host filesystem.

## Install

1. In GitHub, open **Settings → Actions → Runners → New self-hosted runner**, select **Linux / x64**, and generate a short-lived registration token.
2. From the repository checkout on the NAS, run the setup script as root:

   ```sh
   sudo sh ctrlvee-vlc-test/runner/setup-runner.sh
   ```

3. The setup script creates the dedicated DSM account `ctrlvee-runner` if it does not already exist. It generates an unguessable random password that is never displayed or saved, gives the account no DSM application privileges, and refuses to use an account in the `administrators` group.
4. It removes stale per-user ACL entries left by earlier setup attempts, restores traversal-only access on each parent directory, grants read/traverse access to the runner scripts, and grants read-only access to actual media directories and files. It deliberately does not add DENY entries to parent directories: Synology can propagate those entries into child media folders and block fixture access. The runner's ability to list parent-directory names therefore follows the NAS's existing ACLs; this setup does not promise to hide those names. It verifies that the runner can read fixtures but cannot write media directories or files, read the local VLC `.env` file, or access the Docker socket when present. Synology `@eaDir`, recycle-bin, snapshot, and temporary metadata trees are excluded from fixture permissions and checks.
5. The installer prompts for the short-lived registration token **as the unprivileged runner account**. The token is not written to disk or shell history. It then downloads the official GitHub Actions runner v2.338.2 and verifies its pinned SHA-256 checksum. If DSM does not provide `ldd`, the installer creates a private `actions-runner/.local-tools/ldd` compatibility helper backed by DSM's glibc dynamic loader and adds it to `PATH` for runner configuration. It does not modify system libraries or require installing a Linux package.
6. Start the runner:

   ```sh
   sudo -u ctrlvee-runner /bin/sh ctrlvee-vlc-test/runner/run-runner.sh
   ```

The runner receives GitHub's default `self-hosted`, `linux`, and `x64` labels in addition to `vlc-lan`. It has no Docker socket, no Docker-management privileges, and no access to the local VLC password file.

The setup is safe to rerun: it reuses the dedicated account, removes only that account's explicit ACL entries before rebuilding the required permissions, and does not register a second GitHub runner if one is already configured.

## Start at boot

In DSM **Control Panel → Task Scheduler**:

1. Create **Triggered Task → User-defined script**.
2. Set the user to `ctrlvee-runner`.
3. Trigger on **Boot-up**.
4. Set the user-defined script to the absolute path of the repository's `ctrlvee-vlc-test/runner/run-runner.sh`, for example:

   ```sh
   /bin/sh /volume1/docker/ctrlvee/ctrlvee-vlc-test/runner/run-runner.sh
   ```

5. Save and run the task once to verify it starts.

Keep the runner process running in the foreground under Task Scheduler. Do not configure a second copy of the same runner.

## Requirements and security

- Synology DSM on x86_64 with its glibc dynamic loader (the workflow also requires the `x64` label). A system `ldd` command is optional; the installer supplies a runner-local compatibility helper when needed.
- Run the setup script through `sudo` from an interactive SSH terminal.
- Outbound HTTPS access to GitHub.
- DSM `synouser` and `synoacltool`, plus `openssl`, `curl`, `tar`, `sha256sum`, `find`, and `su`.
- The runner account can read/traverse `/volume1/docker/ctrlvee-vlc-test/media`, but cannot write to it.
- The local `ctrlvee-vlc-test/.env` file must not be readable by the runner account.
- The dedicated test VLC container remains bound to host loopback at `127.0.0.1:18080`.
- Restrict write access to the repository's `main` branch. This runner must only execute the manually dispatched trusted-main workflow, never pull-request or fork code.

Runner files and credentials are stored under `actions-runner/`, which is ignored by Git. Never commit that directory, registration tokens, or runner credentials.

To remove the runner, stop its DSM task and process, then use GitHub **Settings → Actions → Runners** to remove the offline runner entry. After that, remove the local `actions-runner/` directory and, if no longer needed, delete the `ctrlvee-runner` DSM account through DSM.
