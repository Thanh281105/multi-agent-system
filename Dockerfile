FROM node:24.19.0-bookworm-slim AS frontend-builder

ENV NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false

WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend ./
RUN npm run build


FROM python:3.12.14-slim-bookworm AS builder

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv "$VIRTUAL_ENV"

COPY pyproject.toml README.md requirements.lock ./
COPY app ./app
COPY --from=frontend-builder /build/app/frontend/dist ./app/frontend/dist

RUN pip install --no-cache-dir --no-compile -r requirements.lock \
    && pip install --no-cache-dir --no-compile --no-build-isolation --no-deps . \
    && python -c "import app, fastapi, sqlalchemy"


FROM python:3.12.14-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="Vietnamese E-commerce Multi-Agent" \
      org.opencontainers.image.description="Evidence-first multi-agent decision platform" \
      org.opencontainers.image.version="1.0.0"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=10001:10001 alembic.ini ./alembic.ini
COPY --chown=10001:10001 migrations ./migrations

EXPOSE 8000

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=3).read()"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
