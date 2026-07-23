"""Service health routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    environment: str


router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Report that the HTTP application is ready to accept requests."""

    # Keep this response free of database and Runtime state. It is a liveness
    # probe, not a claim that every external dependency is healthy.
    return HealthResponse(
        status="ok",
        service="mutiai-core",
        environment=request.app.state.settings.app_env,
    )
