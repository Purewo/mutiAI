"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from threading import RLock
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from mutiai.api.errors import install_error_handlers
from mutiai.api.router import api_router
from mutiai.bootstrap import seed_development_admin
from mutiai.config import Settings, get_settings
from mutiai.db import Database
from mutiai.migrations import upgrade_database
from mutiai.orchestration import TaskOrchestrator
from mutiai.runtime import (
    AgentRuntimeAdapter,
    CodexRuntimeAdapter,
    CodexRuntimeSupervisor,
    FakeRuntimeAdapter,
    RuntimeWorkspaceBinding,
    WorkspaceManager,
    require_codex_app_server_ready,
)
from mutiai.services.approvals import RuntimeApprovalCoordinator
from mutiai.services.assistant import PlatformAssistantService
from mutiai.services.workspaces import WorkspaceProvisioner


def create_app(
    settings: Settings | None = None,
    runtime_adapter: AgentRuntimeAdapter | None = None,
    assistant_runtime_adapter: AgentRuntimeAdapter | None = None,
) -> FastAPI:
    """Build an application with isolated configuration and database state."""

    resolved_settings = settings or get_settings()
    database = Database(resolved_settings)
    product_mutation_lock = RLock()
    workspace_manager = WorkspaceManager(resolved_settings.runtime_workspace_root)
    workspace_provisioner = WorkspaceProvisioner(workspace_manager)
    managed_codex_endpoint: str | None = None
    resolved_runtime_adapter = runtime_adapter

    def require_explicit_workspace(execution_id: str) -> RuntimeWorkspaceBinding:
        raise RuntimeError(
            f"Codex execution requires a product-owned Workspace: {execution_id}"
        )

    if resolved_runtime_adapter is None:
        if resolved_settings.runtime_provider == "codex":
            codex_home = workspace_provisioner.ensure_codex_home()
            managed_codex_endpoint = resolved_settings.codex_app_server_endpoint
            resolved_runtime_adapter = CodexRuntimeAdapter(
                workspace_manager=workspace_manager,
                resolve_workspace=require_explicit_workspace,
                codex_home=codex_home,
                app_server_endpoint=managed_codex_endpoint,
                model=resolved_settings.codex_model,
                capacity_cache_seconds=(
                    resolved_settings.runtime_provider_capacity_cache_seconds
                ),
            )
        else:
            resolved_runtime_adapter = FakeRuntimeAdapter()
    resolved_assistant_adapter = assistant_runtime_adapter
    if resolved_assistant_adapter is None:
        assistant_provider = resolved_settings.assistant_runtime_provider
        if assistant_provider == "inherit":
            resolved_assistant_adapter = resolved_runtime_adapter
        elif assistant_provider == "fake":
            resolved_assistant_adapter = FakeRuntimeAdapter()
        elif isinstance(resolved_runtime_adapter, CodexRuntimeAdapter):
            resolved_assistant_adapter = resolved_runtime_adapter
        else:
            codex_home = workspace_provisioner.ensure_codex_home()
            managed_codex_endpoint = resolved_settings.codex_app_server_endpoint
            resolved_assistant_adapter = CodexRuntimeAdapter(
                workspace_manager=workspace_manager,
                resolve_workspace=require_explicit_workspace,
                codex_home=codex_home,
                app_server_endpoint=managed_codex_endpoint,
                model=resolved_settings.assistant_model
                or resolved_settings.codex_model,
                approval_policy="never",
                capacity_cache_seconds=(
                    resolved_settings.runtime_provider_capacity_cache_seconds
                ),
            )
    approval_coordinator = RuntimeApprovalCoordinator(
        database,
        mutation_lock=product_mutation_lock,
    )
    task_orchestrator = TaskOrchestrator(
        database,
        resolved_settings,
        resolved_runtime_adapter,
        workspace_provisioner,
        mutation_lock=product_mutation_lock,
    )
    task_orchestrator.set_approval_canceller(approval_coordinator.cancel_task)
    runtime_supervisor = (
        CodexRuntimeSupervisor(resolved_runtime_adapter, task_orchestrator)
        if isinstance(resolved_runtime_adapter, CodexRuntimeAdapter)
        else None
    )
    if runtime_supervisor is not None:
        task_orchestrator.set_runtime_watch(runtime_supervisor.watch)
    if isinstance(resolved_runtime_adapter, CodexRuntimeAdapter):
        resolved_runtime_adapter.set_approval_handler(
            approval_coordinator.request_approval
        )
    platform_assistant = PlatformAssistantService(
        database,
        resolved_settings,
        resolved_assistant_adapter,
        workspace_manager,
        task_orchestrator,
        approval_coordinator=approval_coordinator,
        mutation_lock=product_mutation_lock,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            if managed_codex_endpoint is not None:
                require_codex_app_server_ready(
                    managed_codex_endpoint,
                    timeout=(resolved_settings.codex_app_server_ready_timeout_seconds),
                )
            if resolved_settings.database_auto_migrate:
                upgrade_database(resolved_settings.database_url)
            if (
                resolved_settings.bootstrap_admin_enabled
                and resolved_settings.app_env != "production"
            ):
                with database.session() as session:
                    seed_development_admin(session, resolved_settings)
            if runtime_supervisor is not None and isinstance(
                resolved_runtime_adapter, CodexRuntimeAdapter
            ):
                task_orchestrator.recover_orphaned_runtime_executions(
                    is_active=resolved_runtime_adapter.is_active,
                    try_recover=resolved_runtime_adapter.recover,
                )
                approval_coordinator.recover_orphaned_approvals()
            task_orchestrator.resume_deferred_runtime_executions()
            platform_assistant.recover_incomplete_actions()
            platform_assistant.recover_incomplete_turns()
            yield
        finally:
            platform_assistant.close()
            approval_coordinator.close()
            if runtime_supervisor is not None:
                runtime_supervisor.close()
            elif isinstance(resolved_assistant_adapter, CodexRuntimeAdapter):
                resolved_assistant_adapter.close()
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
    app.state.runtime_adapter = resolved_runtime_adapter
    app.state.assistant_runtime_adapter = resolved_assistant_adapter
    app.state.platform_assistant = platform_assistant
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
