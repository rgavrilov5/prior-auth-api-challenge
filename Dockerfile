FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home api

FROM base AS test
RUN uv sync --frozen
COPY tests ./tests
USER api
CMD [".venv/bin/pytest", "-q"]

FROM base AS api
USER api
EXPOSE 8000
CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
