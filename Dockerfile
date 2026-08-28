# syntax=docker/dockerfile:1.6

# ---- Stage 1: builder ----
FROM python:3.11-slim AS builder

WORKDIR /build

# Build-time deps: git for any source installs, gcc just in case a wheel needs
# a C compiler (lxml ships manylinux wheels so this is belt-and-braces).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /build/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /build/requirements.txt


# ---- Stage 2: runtime ----
FROM python:3.11-slim AS runtime

# tini          -> proper PID 1 signal handling (clean `docker stop`)
# tzdata        -> TZ env var works without an image rebuild
# ca-certificates -> HTTPS to auctions.com.au / static.auctions.com.au
# curl          -> healthcheck hits /healthz
# libnss3 / libnspr4 / libatk1.0 / libatk-bridge2.0 / libcups2 / libdrm2 /
# libxkbcommon0 / libxcomposite1 / libxdamage1 / libxfixes3 / libxrandr2 /
# libgbm1 / libasound2 / libpango-1.0-0 / libcairo2 / fonts-liberation ->
#   Playwright's Chromium runtime shared libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        tzdata \
        ca-certificates \
        curl \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpango-1.0-0 \
        libcairo2 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user with uid 1000 to match the host user when using
# bind-mounted ./data (the most common deployment). This avoids permission
# errors on the SQLite db, images, reports, and logs.
RUN groupadd -r -g 1000 auction && useradd -r -g auction -u 1000 -d /app -s /sbin/nologin auction

WORKDIR /app

# Install Python deps from the wheels we built (faster + reproducible).
COPY --from=builder /wheels /wheels
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r /app/requirements.txt \
    && rm -rf /wheels

# Install the Chromium browser binaries for Playwright. We skip --with-deps
# because we already installed the required shared libraries above, and
# --with-deps tries to install font packages that are not available on
# Debian Trixie (ttf-unifont, ttf-ubuntu-font-family).
RUN python -m playwright install chromium \
    && rm -rf /var/lib/apt/lists/*

# Copy the project
COPY . /app/

# Make sure data + log dirs exist and are owned by the runtime user.
RUN mkdir -p /app/data/reports /app/data/logs /app/data/images \
    && chown -R auction:auction /app

USER auction

# Healthcheck: the web UI's plain-text endpoint. The container reports
# unhealthy if Flask is not responding within 30s.
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WEB_PORT:-8080}/healthz" || exit 1

# Use tini as PID 1 for clean signal handling (so SIGTERM shuts the scheduler
# and the web server down gracefully).
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default: run the web UI plus the in-process scheduler. Override with
# `command:` in docker-compose for one-shot cycles:
#   command: ["python", "-m", "auction_tracker", "discover"]
#   command: ["python", "-m", "auction_tracker", "scan"]
#   command: ["python", "-m", "auction_tracker", "heartbeat"]
#   command: ["python", "-m", "auction_tracker", "finalize"]
CMD ["python", "-m", "auction_tracker", "serve"]
