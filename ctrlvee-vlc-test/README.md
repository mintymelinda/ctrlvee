# CtrlVee VLC test instance

This folder manages the dedicated, headless VLC instance used by the optional LAN integration tests. It is separate from production VLC/Plex. The Compose file uses format 3.7 and supports legacy `docker-compose` (Compose V1) and `docker compose` (Compose V2).

## First-time setup

1. Create the dedicated host media directory and place at least one supported audio/video file in it:

       mkdir -p /volume1/docker/ctrlvee/ctrlvee-vlc-test/media

   The remote integration test reads existing files from this directory. The runner needs read/traverse access only; it does not write, create, rename, or delete media files. The Debian runner container uses the numeric UID/GID of the existing `ctrlvee-runner` DSM account so its existing Synology ACLs apply. Both the runner and VLC mount the media directory read-only.

2. Copy the example environment file and set a unique password:

       cp .env.example .env
       chmod 600 .env

   Edit `.env` and replace `VLC_TEST_PASSWORD`. This password is used by the VLC service and is not passed into the Actions runner container. Keep the actual file private; it is ignored by Git.

3. Validate and start only the VLC service:

       docker-compose config --quiet
       docker-compose up -d --build vlc-test

Use `docker compose` instead of `docker-compose` if Compose V2 is installed.

The VLC HTTP control interface is published only on `127.0.0.1:18080` on the Synology host. Do not expose this port through QuickConnect, a reverse proxy, Tailscale Funnel, or router port forwarding.

The Compose file uses the fixed host media path `/volume1/docker/ctrlvee/ctrlvee-vlc-test/media`. The runner container mounts that same path read-only; VLC mounts it at `/media`, also read-only.

## Day-to-day VLC management

Run these commands from this directory:

| Action | Command |
| --- | --- |
| Start | `docker-compose up -d vlc-test` |
| Rebuild and start | `docker-compose up -d --build vlc-test` |
| Status | `docker-compose ps vlc-test` |
| Follow logs | `docker-compose logs -f vlc-test` |
| Stop | `docker-compose stop vlc-test` |
| Start after stopping | `docker-compose start vlc-test` |
| Restart | `docker-compose restart vlc-test` |
| Stop and remove container/network | `docker-compose down` |

`docker-compose down` does not delete the host media directory or the local `.env` file. The runner uses a separate named volume and is managed independently; see [the runner guide](runner/README.md).

## Verify the endpoint locally

From this directory on the Synology host:

    set -a
    . ./.env
    set +a
    curl --fail --silent --show-error \
      --user ":$VLC_TEST_PASSWORD" \
      http://127.0.0.1:18080/requests/status.xml

A successful response is VLC status XML.

## GitHub Actions settings

Configure repository secret `REMOTE_VLC_PASSWORD` with the same value as `VLC_TEST_PASSWORD`.

The manually dispatched remote integration test enumerates supported media files already present in `/volume1/docker/ctrlvee/ctrlvee-vlc-test/media`, asks VLC to play each one, checks playback controls, and removes only the playlist entries it created. It does not modify media files or pre-existing playlist entries.

Never point this service at production media or a production VLC endpoint. This instance should remain dedicated to testing.
