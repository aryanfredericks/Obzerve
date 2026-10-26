# Obzerve server image: runs the API (default command) or a queue worker
# (`obzerve-server worker`). Build from the repository root:
#   podman build -f server/Dockerfile -t obzerve-server .

FROM ghcr.io/astral-sh/uv:0.12.9 AS uv

FROM docker.io/library/python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first, from the lockfile and manifests only, so source edits reuse this layer.
# Only the server's dependencies are installed (no PyTorch: that belongs to the demo agent).
COPY uv.lock pyproject.toml .python-version ./
COPY sdk/python/pyproject.toml sdk/python/
COPY server/pyproject.toml server/
COPY examples/research_agent/pyproject.toml examples/research_agent/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package obzerve-server --no-install-workspace
COPY server server
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package obzerve-server --no-editable

FROM docker.io/library/python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home obzerve
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER obzerve
EXPOSE 8000 9101
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["obzerve-server", "serve", "--host", "0.0.0.0", "--port", "8000"]
