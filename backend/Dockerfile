# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

USER 65532:65532
EXPOSE 8000
CMD ["vlytics-api"]
