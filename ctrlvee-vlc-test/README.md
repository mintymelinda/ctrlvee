# CtrlVee VLC test instance

This folder manages the dedicated, headless VLC instance used by the optional LAN integration tests. It is separate from production VLC/Plex.

## First-time setup

1. Create a dedicated host media directory, for example:

       /volume1/docker/ctrlvee-vlc-test/media

   Give the self-hosted GitHub runner account write access. VLC mounts this directory read-only.

2. Copy the example environment file and set a unique password:

       cp .env.example .env
       chmod 600 .env

   Edit `.env` and replace `VLC_TEST_PASSWORD`. Keep the actual file private; it is ignored by Git.

3. Validate and start the service:

       docker compose -f docker-compose.yml config --quiet
       docker compose -f docker-compose.yml up -d --build

The VLC HTTP control interface is published only on `127.0.0.1:18080` on the Synology host. Do not expose this port through QuickConnect, a reverse proxy, Tailscale Funnel, or router port forwarding.

## Day-to-day management

Run these commands from this directory:

| Action | Command |
| --- | --- |
| Start | `docker compose up -d` |
| Rebuild and start | `docker compose up -d --build` |
| Status | `docker compose ps` |
| Follow logs | `docker compose logs -f vlc-test` |
| Stop | `docker compose stop` |
| Start after stopping | `docker compose start` |
| Restart | `docker compose restart` |
| Stop and remove container/network | `docker compose down` |

`docker compose down` does not delete the host media directory or the local `.env` file.

## Verify the endpoint locally

From the Synology host:

    docker compose exec vlc-test true
    curl --fail --silent --show-error \
      --user ":$(grep '^VLC_TEST_PASSWORD=' .env | cut -d= -f2-)" \
      http://127.0.0.1:18080/requests/status.xml

A successful response is VLC status XML.

## GitHub Actions settings

Configure these repository Actions settings to match `.env`:

- Secret `REMOTE_VLC_PASSWORD`: same value as `VLC_TEST_PASSWORD`.
- Variable `REMOTE_VLC_MEDIA_DIR`: exact host path from `VLC_TEST_MEDIA_DIR`.

The self-hosted runner and this Compose stack must share the same Synology host directory. The runner creates uniquely named media fixtures there; the container sees those files under `/media`.

Never point this service at production media or a production VLC endpoint. The remote integration test is designed to remove only its own playlist entry; this instance should remain dedicated to testing.
