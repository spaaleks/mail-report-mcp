FROM ghcr.io/astral-sh/uv:0.9-python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
COPY tests ./tests
COPY assets ./assets
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini libcairo2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 mailreport

WORKDIR /app
COPY --from=builder --chown=mailreport:mailreport /app /app

RUN mkdir -p /config /data/attachments /data/state /data/logo \
    && chown -R mailreport:mailreport /config /data \
    && chmod 700 /data/state

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

USER mailreport
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('MCP_PORT','8000')}/healthz\",timeout=4).read()"]

ENTRYPOINT ["/usr/bin/tini", "--", "mail-report"]
