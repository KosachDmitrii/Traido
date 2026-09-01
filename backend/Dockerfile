# syntax=docker/dockerfile:1

# Multi-stage: build wheels once, ship a slim runtime that never carries a compiler.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY agents ./agents
COPY api ./api
COPY broker ./broker
COPY core ./core
COPY database ./database
COPY market_data ./market_data
COPY notifications ./notifications
COPY quant ./quant
COPY risk ./risk
COPY trading ./trading
COPY universe ./universe
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY configs ./configs

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install "psycopg[binary]>=3.2" \
    && /opt/venv/bin/pip install .


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRAIDO_ENV=production

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 traido

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=traido:traido . .

# The desk writes the kill-switch flag and the local journal here.
RUN mkdir -p /app/data && chown -R traido:traido /app/data \
    && chmod +x /app/scripts/railway-start.sh

USER traido
EXPOSE 8000

# Readiness, not liveness: the container is only "healthy" once its
# dependencies answer. Start period covers migrations on first boot.
# Railway uses deploy.healthcheckPath from railway.toml instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health/ready" || exit 1

CMD ["./scripts/railway-start.sh"]
