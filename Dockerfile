# syntax=docker/dockerfile:1

# ---- build stage: resolve and install dependencies with uv from the lockfile ----
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so this layer stays cached until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY baaki ./baaki
COPY migrations ./migrations
COPY alembic.ini README.md ./
RUN uv sync --frozen --no-dev

# ---- runtime stage: no compiler, no uv, no tests, non-root ----
FROM python:3.12-slim AS runtime
ARG GIT_SHA=dev
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_SHA=$GIT_SHA
LABEL org.opencontainers.image.source="https://github.com/7arj/baaki" \
      org.opencontainers.image.revision=$GIT_SHA

RUN groupadd --system --gid 10001 baaki && useradd --system --uid 10001 --gid baaki --no-create-home baaki
WORKDIR /app
COPY --from=build --chown=root:root /app /app
# The only writable path: the SQLite fallback when DATABASE_URL is unset (local runs).
RUN mkdir -p /app/data && chown baaki:baaki /app/data

USER 10001
EXPOSE 8080
# --proxy-headers: trust X-Forwarded-Proto from the ingress so the app knows the user came in over https.
# --no-access-log: the app writes its own structured JSON access log (baaki/app/ops.py).
CMD ["uvicorn", "baaki.app.web:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips=*", "--no-access-log"]
