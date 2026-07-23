"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from mutiai.api import api_router
from mutiai.api.errors import install_error_handlers
from mutiai.bootstrap import seed_development_admin
from mutiai.config import Settings, get_settings
from mutiai.db import Database
from mutiai.migrations import upgrade_database
from mutiai.orchestration import TaskOrchestrator
from mutiai.runtime import AgentRuntimeAdapter, WorkspaceManager


def create_app(
    settings: Settings | None = None,
    runtime_adapter: AgentRuntimeAdapter | None = None,
) -> FastAPI:
    """Build an application with isolated configuration and database state."""

    resolved_settings = settings or get_settings()
    database = Database(resolved_settings)
    task_orchestrator = TaskOrchestrator(
        database,
        resolved_settings,
        runtime_adapter,
    )
    workspace_manager = WorkspaceManager(resolved_settings.runtime_workspace_root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if resolved_settings.database_auto_migrate:
            upgrade_database(resolved_settings.database_url)
        if (
            resolved_settings.bootstrap_admin_enabled
            and resolved_settings.app_env != "production"
        ):
            with database.session() as session:
                seed_development_admin(session, resolved_settings)
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
    app.state.task_orchestrator = task_orchestrator
    app.state.workspace_manager = workspace_manager
    install_error_handlers(app)

    @app.middleware("http")
    async def attach_request_id(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = request.headers.get("X-Request-ID")
        if not request_id or len(request_id) > 128:
            request_id = str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(api_router)

    return app


app = create_app()
