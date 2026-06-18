# Dockerfile for KritP/freshrss-mcp — Centaur rss stack sidecar
# Build context: this directory.
#   docker build -t kritp/freshrss-mcp:latest .
# Used by /home/ubuntu/repo/rss/docker-compose.yml as the `freshrss-mcp` service.
#
# Image strategy: uv-managed Python on a slim base. Multi-stage keeps the
# runtime image small (no uv, no git, no build tools).

# ── Stage 1: build ───────────────────────────────────────────────────────────
FROM python:3.13-slim AS build

# uv pinned for reproducibility. Bump intentionally.
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /usr/local/bin/

WORKDIR /app

# Layer cache: dependency install before source copy.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev && \
    uv run python -c "import freshrss_mcp; print('ok')"


# ── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# Run as non-root. UID 1000 matches the typical `ubuntu` user on Centaur
# but stays inside the container — no host UID collision.
RUN groupadd --system --gid 1000 freshrss && \
    useradd  --system --uid 1000 --gid freshrss --no-create-home freshrss

COPY --from=build --chown=freshrss:freshrss /app /app

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER freshrss

EXPOSE 8000

# Streamable HTTP transport. Host/port configurable via MCP_SERVER_HOST/PORT.
# Default 0.0.0.0:8000 inside the container; rss/docker-compose.yml publishes
# 100.91.202.122:8005->8000 on the Tailscale-only interface.
CMD ["freshrss-mcp"]
