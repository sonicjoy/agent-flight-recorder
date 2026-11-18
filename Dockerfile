# The recorder's ingest API (`afr serve`), for `docker compose --profile api` and Cloud Run (#27).
# The MCP server isn't in this image: it runs on the user's machine over stdio (#24).
#
# Base images are pinned by digest and at least two weeks old (python 3.12.15-slim-bookworm
# built 2026-10-21, uv 0.13.7 built 2026-11-03). To update: pull the new tags, check their
# build dates, and replace both tag and digest.

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# ---- Build: the locked dependencies, then the project, into /app/.venv --------------------
FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first, in their own layer: a code change doesn't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- Run: the venv only, as a user without a login or root ------------------------------
FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
# AFR_HOME exists in the image, owned by afr, so a volume mounted there starts with that owner.
RUN useradd --create-home --shell /usr/sbin/nologin afr \
    && install -d -m 0700 -o afr -g afr /home/afr/.afr
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    AFR_HOME=/home/afr/.afr
USER afr
WORKDIR /home/afr
EXPOSE 4318
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '4318')}/healthz\", timeout=2)"]
# Cloud Run sets PORT. Listening on all interfaces needs API keys (AFR_API_KEY_HASHES) or, for
# a local container only, AFR_INSECURE_NO_AUTH=1: `afr serve` refuses to start otherwise.
CMD ["sh", "-c", "exec afr serve --host 0.0.0.0 --port \"${PORT:-4318}\""]
