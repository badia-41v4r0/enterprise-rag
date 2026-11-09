"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
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
    setup,
    tools,
)
from app.api.deps import setup_gate
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
    # Always reachable, also in setup mode (ADR 0034): health, public info, login and the setup wizard itself.
    app.include_router(health.router)
    app.include_router(setup.public_router)
    app.include_router(auth.router)
    app.include_router(setup.wizard_router)
    # Everything else answers 503 "setup required" while the installation is in setup mode, except for the
    # administrator created in the wizard (who configures the rest through the normal admin screens).
    gated = [Depends(setup_gate)]
    app.include_router(setup.assistant_router, dependencies=gated)
    app.include_router(admin.router, dependencies=gated)
    app.include_router(documents.router, dependencies=gated)
    app.include_router(chat.router, dependencies=gated)
    app.include_router(tools.router, dependencies=gated)
    app.include_router(secrets.router, dependencies=gated)
    # After `secrets`: its /providers/openrouter routes must match before /providers/{provider_id}.
    app.include_router(providers.router, dependencies=gated)
    app.include_router(connectors.router, dependencies=gated)
    app.include_router(mcp_registry.router, dependencies=gated)
    app.include_router(quotas.router, dependencies=gated)
    app.include_router(model_router.router, dependencies=gated)

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
