"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from threading import RLock
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
from mutiai.runtime import (
    AgentRuntimeAdapter,
    CodexRuntimeAdapter,
    CodexRuntimeSupervisor,
    WorkspaceManager,
)
from mutiai.services.approvals import RuntimeApprovalCoordinator
from mutiai.services.workspaces import WorkspaceProvisioner


def create_app(
    settings: Settings | None = None,
    runtime_adapter: AgentRuntimeAdapter | None = None,
) -> FastAPI:
    """Build an application with isolated configuration and database state."""

    resolved_settings = settings or get_settings()
    database = Database(resolved_settings)
    product_mutation_lock = RLock()
    workspace_manager = WorkspaceManager(resolved_settings.runtime_workspace_root)
    workspace_provisioner = WorkspaceProvisioner(workspace_manager)
    approval_coordinator = RuntimeApprovalCoordinator(
        database,
        mutation_lock=product_mutation_lock,
    )
    task_orchestrator = TaskOrchestrator(
        database,
        resolved_settings,
        runtime_adapter,
        workspace_provisioner,
        mutation_lock=product_mutation_lock,
    )
    runtime_supervisor = (
        CodexRuntimeSupervisor(runtime_adapter, task_orchestrator)
        if isinstance(runtime_adapter, CodexRuntimeAdapter)
        else None
    )
    if runtime_supervisor is not None:
        task_orchestrator.set_runtime_watch(runtime_supervisor.watch)
    if isinstance(runtime_adapter, CodexRuntimeAdapter):
        runtime_adapter.set_approval_handler(
            approval_coordinator.request_approval
        )

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
        if runtime_supervisor is not None and isinstance(
            runtime_adapter, CodexRuntimeAdapter
        ):
            task_orchestrator.recover_orphaned_runtime_executions(
                is_active=runtime_adapter.is_active,
            )
            approval_coordinator.recover_orphaned_approvals()
        try:
            yield
        finally:
            approval_coordinator.close()
            if runtime_supervisor is not None:
                runtime_supervisor.close()
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
    app.state.approval_coordinator = approval_coordinator
    app.state.workspace_manager = workspace_manager
    app.state.workspace_provisioner = workspace_provisioner
    app.state.runtime_supervisor = runtime_supervisor
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
