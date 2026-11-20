# syntax=docker/dockerfile:1
# heliopuddle image: API, UI, services and embedded Prefect in one process.
# Build from the repository root:  docker build -f docker/Dockerfile -t heliopuddle .
# Published to Docker Hub by .github/workflows/docker.yml; also the e2e test image.
# See docs/design/deployment.md ("Docker image").

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.11.32

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------- builder ---
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src

# Behind a TLS-intercepting proxy, pass its CA bundle as an optional build
# secret:  docker build --secret id=ca,src=/path/to/ca-bundle.crt ...

# Dependencies first (cached unless the lock file changes).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv sync --frozen --no-dev --extra all --no-install-project

# Then the project itself, installed non-editable so /src is not needed at run time.
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv sync --frozen --no-dev --extra all --no-editable

# ---------------------------------------------------------------- runtime ---
FROM python:${PYTHON_VERSION}-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

ARG APP_UID=10001
RUN groupadd --system --gid ${APP_UID} heliopuddle \
    && useradd --system --uid ${APP_UID} --gid heliopuddle \
        --home-dir /var/lib/heliopuddle --no-create-home --shell /usr/sbin/nologin heliopuddle \
    && mkdir -p /data /config /var/lib/heliopuddle \
    && chown heliopuddle:heliopuddle /data /config /var/lib/heliopuddle

COPY --from=builder /opt/venv /opt/venv
COPY --chown=heliopuddle:heliopuddle examples/projects/ /config/

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/heliopuddle \
    HELIOPUDDLE_CONFIG_DIR=/config \
    HELIOPUDDLE_DATA_DIR=/var/lib/heliopuddle \
    PREFECT_UI_STATIC_DIRECTORY=/var/lib/heliopuddle/prefect/ui \
    PREFECT_SERVER_API_HOST=0.0.0.0

USER heliopuddle
WORKDIR /var/lib/heliopuddle
VOLUME ["/data", "/config", "/var/lib/heliopuddle"]

# 8080: API (/api/v1) and UI (/). 4200: embedded Prefect server and UI.
EXPOSE 8080 4200

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/api/v1/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["tini", "--", "heliopuddle"]
CMD ["server", "start", "--host", "0.0.0.0", "--port", "8080"]
