FROM node:24-bookworm-slim AS clis
RUN npm install --prefix /opt/clis @anthropic-ai/claude-code@2.1.312 @openai/codex@0.164.0
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/earnings EARNINGS_WATCH_DATA_DIR=/data PATH=/app/.venv/bin:$PATH
RUN useradd --uid 1000 --create-home earnings && mkdir /data && chown earnings /data
WORKDIR /app
FROM base AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
FROM base AS serve
COPY --from=dependencies /app/.venv /app/.venv
USER 1000:1000
VOLUME /data
HEALTHCHECK --interval=60s --timeout=10s CMD ["earnings-watch", "health", "--json"]
ENTRYPOINT ["earnings-watch"]
CMD ["serve", "--http", "--host", "0.0.0.0"]
FROM serve AS worker
USER root
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=clis /usr/local/bin/node /usr/local/bin/node
COPY --from=clis /opt/clis /opt/clis
ENV PATH=/opt/clis/node_modules/.bin:$PATH
USER 1000:1000
CMD ["worker"]
