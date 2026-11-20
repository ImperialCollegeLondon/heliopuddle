# Docker

The published image is `ghcr.io/imperialcollegelondon/heliopuddle`. `:latest` is
the newest release, `:main` follows the main branch and `:pr-<number>` is a pull
request's build.

## docker run

```bash
docker run -p 8080:8080 -p 127.0.0.1:4200:4200 \
  -v $PWD/data:/data -v $PWD/projects:/config \
  -v hp-state:/var/lib/heliopuddle \
  ghcr.io/imperialcollegelondon/heliopuddle:latest
```

| Port | Serves |
|---|---|
| 8080 | the API (`/api/v1`) and the UI (`/`) |
| 4200 | the embedded Prefect server and UI |

!!! warning "The Prefect port has no authentication"
    Anyone who can reach port 4200 can start, cancel and delete flow runs and
    read their logs. Publish it on loopback only (`127.0.0.1:4200:4200`, as
    above) and use an SSH tunnel or an authenticating reverse proxy for remote
    access.

| Volume | Holds |
|---|---|
| `/var/lib/heliopuddle` | state: the SQLite database, the embedded Prefect database and the signing key. Keep it. |
| `/data` | files, for the image's example project |
| `/config` | project YAML files; the image ships an example project |

The container runs as uid 10001, so a host directory mounted for `/data` must be
writable by that user. Pass settings with `-e HELIOPUDDLE_...` or `--env-file`.
The first admin's password is logged once at first start.

## Docker Compose with Postgres and Prefect

This runs heliopuddle against an external Postgres and a separate Prefect
server (hp stops at startup with a clear message if either is unreachable).

```yaml
services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: heliopuddle
      POSTGRES_USER: heliopuddle
      POSTGRES_PASSWORD: change-me
    volumes:
      - pg-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U heliopuddle -d heliopuddle"]
      interval: 5s
      timeout: 5s
      retries: 20

  prefect:
    image: prefecthq/prefect:3.9-python3.12
    restart: unless-stopped
    command: ["prefect", "server", "start", "--host", "0.0.0.0"]
    environment:
      # The address your browser uses for the Prefect UI's API calls.
      PREFECT_UI_API_URL: http://localhost:4200/api
    ports:
      - "127.0.0.1:4200:4200"   # no authentication: keep it off public interfaces
    volumes:
      - prefect-data:/root/.prefect
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:4200/api/health', timeout=4)"]
      interval: 5s
      timeout: 5s
      retries: 30
      start_period: 10s

  heliopuddle:
    image: ghcr.io/imperialcollegelondon/heliopuddle:latest
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
      prefect:
        condition: service_healthy
    ports:
      - "8080:8080"
    environment:
      HELIOPUDDLE_DATABASE_URL: postgresql+psycopg://heliopuddle:change-me@postgres:5432/heliopuddle
      PREFECT_API_URL: http://prefect:4200/api
      HELIOPUDDLE_PREFECT_UI_URL: http://localhost:4200
    volumes:
      - hp-state:/var/lib/heliopuddle
      - hp-data:/data
      # - ./projects:/config:ro

volumes:
  pg-data:
  prefect-data:
  hp-state:
  hp-data:
```

For a single container with SQLite and the embedded Prefect server, use the
`docker run` command above. Both setups are in the repository's `docker/`
directory (`compose.yaml` and `compose.external.yaml`), together with
`heliopuddle.env.example`, which lists the optional settings. See
[Environment variables](environment.md) for all of them.
