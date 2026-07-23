"""Stable public API error envelope."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


logger = logging.getLogger(__name__)


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    request_id: str
    details: Any | None = None


@dataclass(slots=True)
class ApiError(Exception):
    status_code: int
    code: str
    message: str
    details: Any | None = None


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid4()))


def _response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        code=code,
        message=message,
        request_id=_request_id(request),
        details=details,
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(exclude_none=True),
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "location": list(error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return _response(
            request,
            status_code=422,
            code="INVALID_REQUEST",
            message="The request payload is invalid.",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _response(
            request,
            status_code=exc.status_code,
            code="HTTP_ERROR",
            message=str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled API error", exc_info=exc)
        return _response(
            request,
            status_code=500,
            code="INTERNAL_ERROR",
            message="An unexpected server error occurred.",
        )
