# syntax=docker/dockerfile:1
# Demo MCP server image for the isolated runner (ADR 0022). The runner only starts images whose ID is
# pinned in ERAG_RUNNER_IMAGES. The server listens on a Unix socket: the container has no network.
FROM python:3.12-slim
RUN pip install --no-cache-dir "mcp==2.3.0" "uvicorn==0.54.0" \
    && useradd --system --uid 10001 --no-create-home mcp
COPY mcp_servers/__init__.py mcp_servers/serve.py /app/mcp_servers/
COPY mcp_servers/office_server /app/mcp_servers/office_server
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
CMD ["python", "-m", "mcp_servers.office_server", "--uds", "/run/mcp/mcp.sock"]
