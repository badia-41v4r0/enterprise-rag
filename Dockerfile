# syntax=docker/dockerfile:1

# ---- Python app -------------------------------------------------------------------------------
FROM python:3.12-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md LICENSE NOTICE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend ./backend
COPY mcp_servers ./mcp_servers
COPY demo ./demo
COPY alembic.ini ./
RUN uv sync --frozen --no-dev \
    && useradd --system --uid 10001 --home /app erag \
    && mkdir -p /data/blobs && chown -R erag /data
USER erag
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
