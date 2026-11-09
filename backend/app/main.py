"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin,
    auth,
    chat,
    connectors,
    documents,
    health,
    mcp_registry,
    model_router,
    providers,
    quotas,
    secrets,
    tools,
)
from app.core.config import get_settings, warn_removed_settings
from app.core.db import dispose_engine
from app.core.logging import configure_logging
from app.core.middleware import SecurityHeadersMiddleware


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    settings.check_secrets()
    configure_logging(settings.log_level, settings.log_json)
    warn_removed_settings()

    app = FastAPI(title="EnterpriseRAG", version="0.1.0.dev0", lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(documents.router)
    app.include_router(chat.router)
    app.include_router(tools.router)
    app.include_router(secrets.router)
    # After `secrets`: its /providers/openrouter routes must match before /providers/{provider_id}.
    app.include_router(providers.router)
    app.include_router(connectors.router)
    app.include_router(mcp_registry.router)
    app.include_router(quotas.router)
    app.include_router(model_router.router)

    _mount_frontend(app, settings.frontend_dist)
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built React SPA from the same origin (cookie auth, no CORS)."""
    index = dist / "index.html"
    if not index.exists():
        return
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    def spa(path: str) -> FileResponse | JSONResponse:
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
