# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src ./src
COPY backend/migrations ./migrations
COPY config/variants.toml ./config/variants.toml
RUN uv sync --frozen --no-dev

USER 65532:65532
EXPOSE 8000
CMD ["vlytics-api"]
