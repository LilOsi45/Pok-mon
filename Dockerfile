FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin

WORKDIR /app

# curl for the container healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Set INSTALL_PLAYWRIGHT=false for a much smaller image if you don't need
# the Playwright fetcher (JS-heavy/anti-bot shops).
ARG INSTALL_PLAYWRIGHT=true
RUN pip install ".[postgres]" \
    && if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
         pip install ".[playwright]" \
         && playwright install --with-deps chromium \
         && rm -rf /var/lib/apt/lists/*; \
       fi

RUN mkdir -p /app/data
VOLUME /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
