"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from mutiai.api import api_router
from mutiai.config import Settings, get_settings
from mutiai.db import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application with isolated configuration and database state."""

    resolved_settings = settings or get_settings()
    database = Database(resolved_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            database.dispose()

    app = FastAPI(
        title="mutiAI Core API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = database
    app.include_router(api_router)

    return app


app = create_app()
