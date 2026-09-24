# syntax=docker/dockerfile:1.6
#
# TestFortge — production container image.
#
# Base image is the official Microsoft Playwright Python image pinned to the
# exact Playwright version used by the app (see requirements.txt). It ships:
#   * Python 3.x with pip
#   * Chromium / Firefox / WebKit browsers
#   * All transitive OS libraries (fonts, nss, glib, etc.) needed to run them
#   * A non-root user named `pwuser` (UID 1000)
#
# Using this image keeps the Dockerfile short and guarantees the container
# browser matches the playwright python package exactly — a frequent source
# of "launch failed" errors with DIY installs.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

# ── OCI labels ────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="TestFortge"
LABEL org.opencontainers.image.description="Flask-based QA test-case / checklist / automation framework"
LABEL org.opencontainers.image.source="https://github.com/"

# ── Python runtime tuning ─────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ── System packages ──────────────────────────────────────────────
# poppler-utils provides `pdftoppm` which pdf2image shells out to when
# rasterising PDF mockups for the /estimation Mockups tab. Without it
# pdf2image throws PDFInfoNotInstalledError and the route silently
# falls back to text-only PDF rendering — losing the visual signal
# vision analysis depends on. The package is ~7 MB and adds no runtime
# overhead when no PDFs are uploaded.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && rm -rf /var/lib/apt/lists/*

# ── App layout ────────────────────────────────────────────────────
WORKDIR /app

# Install Python deps first so layer caching survives source edits.
# gunicorn is added only in the container runtime; local dev still uses
# `python app.py` (Flask's built-in server) via requirements.txt.
#
# The MCP server reqs (`mcp` + `uvicorn`) install alongside so the same
# image can boot either the Flask service or the MCP HTTP service. The
# choice is made by the Render service's start command (see render.yaml).
COPY requirements.txt ./
COPY mcp_server/requirements.txt ./mcp_server_requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "gunicorn==23.0.0" \
 && pip install --no-cache-dir -r ./mcp_server_requirements.txt

# Copy the application. .dockerignore filters caches, tests, VCS, etc.
COPY . .

# Volumes for session store / generated artefacts / uploads.
# All three live under /app/data because the launchpad backend mounts exactly
# one persistent volume, on that path, chowned to uid 1000. Anything written
# outside it is lost on redeploy. The config module recreates the subdirectories
# at import time, which is what makes this survive the mount shadowing whatever
# the image baked in.
RUN mkdir -p /app/data/storage /app/data/flask_session /app/data/uploads \
 && chown -R pwuser:pwuser /app

VOLUME ["/app/data"]

# Drop privileges. The playwright image ships pwuser (uid 1000) preconfigured
# with the browser sandboxing caps it needs.
USER pwuser

# ── Runtime configuration (overridable) ───────────────────────────
# SECRET_KEY is intentionally NOT baked in — it must be provided at run time
# via `-e SECRET_KEY=...` or a compose env_file. Starting without one in
# non-debug mode aborts at import (see config.py _resolve_secret_key).
ENV LOG_LEVEL=INFO \
    LOG_FORMAT=json \
    STORAGE_FOLDER=/app/data/storage \
    SESSION_FILE_DIR=/app/data/flask_session \
    UPLOAD_FOLDER=/app/data/uploads \
    PORT=8080

# 8080 is the port the launchpad nginx-router proxies to (LP_CONTAINER_PORT).
# Render overrides the command below and injects its own $PORT, so the two
# environments agree without a second Dockerfile.
EXPOSE 8080

# ── Health check ──────────────────────────────────────────────────
# /healthz returns 200 when session/storage/upload dirs are writable,
# 503 otherwise. --start-period gives the first few seconds for worker
# boot before failures count. Uses python to avoid needing curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
p=os.environ.get('PORT','8080'); \
r=urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz', timeout=3); \
sys.exit(0 if r.status == 200 else 1)" || exit 1

# ── Process ───────────────────────────────────────────────────────
# 2 workers × 4 threads is a sane default for a mixed I/O workload
# (LLM calls, Playwright, file parsing). Tune via compose/env for prod.
# --graceful-timeout matches the JobQueue shutdown hook window so SIGTERM
# lets running automations drain cleanly.
# Shell form so $PORT expands: launchpad fixes it at 8080, Render injects its
# own. `exec` keeps gunicorn as PID 1 so SIGTERM still reaches it and the
# JobQueue shutdown hook gets its graceful window.
CMD ["sh", "-c", "exec gunicorn \
     --bind 0.0.0.0:${PORT:-8080} \
     --workers 2 \
     --threads 4 \
     --worker-class gthread \
     --timeout 120 \
     --graceful-timeout 30 \
     --access-logfile - \
     --error-logfile - \
     app:app"]
