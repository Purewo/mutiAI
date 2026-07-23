"""Top-level API router."""

from fastapi import APIRouter

from mutiai.api.routes.auth import router as auth_router
from mutiai.api.routes.health import router as health_router
from mutiai.api.routes.organizations import router as organizations_router


api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(organizations_router)
