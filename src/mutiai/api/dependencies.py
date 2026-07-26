"""Request-scoped API dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.api.errors import ApiError
from mutiai.config import Settings
from mutiai.models import BrowserSession, User
from mutiai.models.base import utc_now
from mutiai.orchestration import TaskOrchestrator
from mutiai.runtime import WorkspaceManager
from mutiai.security import hash_session_token
from mutiai.services.approvals import RuntimeApprovalCoordinator
from mutiai.services.assistant import PlatformAssistantService


def get_db_session(request: Request) -> Iterator[Session]:
    with request.app.state.database.session() as session:
        yield session


def get_request_settings(request: Request) -> Settings:
    return request.app.state.settings


DbSession = Annotated[Session, Depends(get_db_session)]
RequestSettings = Annotated[Settings, Depends(get_request_settings)]


def get_task_orchestrator(request: Request) -> TaskOrchestrator:
    return request.app.state.task_orchestrator


TaskRunner = Annotated[TaskOrchestrator, Depends(get_task_orchestrator)]


def get_workspace_manager(request: Request) -> WorkspaceManager:
    return request.app.state.workspace_manager


ManagedWorkspaces = Annotated[WorkspaceManager, Depends(get_workspace_manager)]


def get_approval_coordinator(request: Request) -> RuntimeApprovalCoordinator:
    return request.app.state.approval_coordinator


ApprovalManager = Annotated[
    RuntimeApprovalCoordinator,
    Depends(get_approval_coordinator),
]


def get_platform_assistant(request: Request) -> PlatformAssistantService:
    return request.app.state.platform_assistant


PlatformAssistant = Annotated[
    PlatformAssistantService,
    Depends(get_platform_assistant),
]


def require_current_user(
    request: Request,
    session: DbSession,
    settings: RequestSettings,
) -> User:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise ApiError(401, "AUTH_REQUIRED", "Authentication is required.")

    browser_session = session.scalar(
        select(BrowserSession).where(
            BrowserSession.token_hash == hash_session_token(token),
            BrowserSession.revoked_at.is_(None),
            BrowserSession.expires_at > utc_now(),
        )
    )
    if browser_session is None or not browser_session.user.is_active:
        raise ApiError(401, "AUTH_REQUIRED", "Authentication is required.")

    return browser_session.user


CurrentUser = Annotated[User, Depends(require_current_user)]
