# Build stage: resolve and install dependencies with uv into a virtualenv.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Runtime stage: slim image, non-root user, the venv, app code, and the semantic model and eval files.
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 metrics
WORKDIR /app
COPY --from=build --chown=metrics:metrics /app /app
COPY --chown=metrics:metrics semantic ./semantic
COPY --chown=metrics:metrics eval ./eval
# The warehouse is generated here on first start; the non-root user must own it.
RUN mkdir -p /app/data && chown metrics:metrics /app/data
USER metrics
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8840 METRICLAYER_HOME=/app
EXPOSE 8840
HEALTHCHECK --interval=15s --timeout=3s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8840/healthz', timeout=2)"
CMD ["metriclayer", "serve"]
