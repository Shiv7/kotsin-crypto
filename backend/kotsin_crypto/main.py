"""Process entrypoint: FastAPI app + engine lifecycle. One process, one port (docs/adr/0001)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes import router as api_router
from .api.ws import router as ws_router
from .bus import Bus
from .config import Settings, assert_no_unknown_env
from .engine import Engine
from .log import configure_logging
from .ops.telegram import Telegram

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(settings: Settings | None = None, *, frontend_dist: Path = FRONTEND_DIST) -> FastAPI:
    settings = settings or Settings()
    assert_no_unknown_env()
    configure_logging(settings.log_level, json_output=settings.env != "dev")
    log = structlog.get_logger("boot")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        app.state.settings = settings
        app.state.bus = Bus()
        app.state.telegram = Telegram(settings)
        app.state.started_at = time.time()
        log.info(
            "boot",
            version=__version__,
            delta_env=settings.delta_env.value,
            rest=settings.endpoints.rest,
            symbols=settings.symbol_list,
            api_keys=settings.has_api_keys,
            telegram=app.state.telegram.enabled,
        )
        await app.state.telegram.send(
            f"kotsin-crypto {__version__} up · {settings.delta_env.value} · "
            f"{','.join(settings.symbol_list)}"
        )
        app.state.engine = None
        if settings.engine_enabled:
            engine = Engine(settings, app.state.bus, app.state.telegram)
            await engine.start()
            app.state.engine = engine
        try:
            yield
        finally:
            if app.state.engine is not None:
                await app.state.engine.stop()
            await app.state.telegram.send("kotsin-crypto down")
            await app.state.telegram.aclose()

    app = FastAPI(title="kotsin-crypto", version=__version__, lifespan=lifespan)
    app.include_router(api_router, prefix="/api")
    app.include_router(ws_router)
    mount_ui(app, frontend_dist)  # must be last: it registers a catch-all route
    return app


def mount_ui(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA from the same port. Hashed assets are static; every other non-API path
    returns index.html so BrowserRouter deep links (/system, /trades) survive a refresh."""
    if not dist.is_dir():
        return
    dist = dist.resolve()
    index = dist / "index.html"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        candidate = (dist / path).resolve() if path else index
        if candidate != index and candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(index)


def cli() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
