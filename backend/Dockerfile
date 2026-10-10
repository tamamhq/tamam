# syntax=docker/dockerfile:1

# ── Stage 1: build the dependency venv with uv ──────────────────────────────
FROM python:3.12-slim AS builder

# uv: fast, reproducible installs from uv.lock
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install deps first (cached layer) from the lockfile only — no app code yet.
# Prod extras: ios (Appium client to drive the cloud device farm), postgres
# (asyncpg), observability (traces). Web-executor extras are intentionally left
# out to keep the image small and the attack surface minimal.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev \
        --extra ios --extra postgres --extra observability

# Now the app code, then install the project itself.
COPY . .
RUN uv sync --frozen --no-dev \
        --extra ios --extra postgres --extra observability

# ── Stage 2: minimal runtime image, non-root ────────────────────────────────
FROM python:3.12-slim

# Tesseract: the OCR grounder (TAMAM_IOS_GROUNDER=ocr, the default) reads text the accessibility
# tree misses on pixel-only screens. Without the binary the grounder silently does nothing.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user (never root in prod containers).
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

# Own the workdir itself (not just its contents) and pre-create a writable
# artifacts dir. In prod, point TAMAM_ARTIFACTS_DIR at a mounted volume instead.
RUN mkdir -p /app/artifacts && chown app:app /app /app/artifacts

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000

# Default command = API. Bind :: so both the hosting platform's public proxy and its private
# IPv6 network (Caddy → api.internal) can reach it; $PORT is injected.
# The WORKER service overrides this in your hosting platform with:
#   arq tamam.worker.WorkerSettings
CMD ["sh", "-c", "uvicorn tamam.main:app --host :: --port ${PORT:-8000}"]
