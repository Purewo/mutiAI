"""Browser-session authentication routes."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, update

from mutiai.api.dependencies import CurrentUser, DbSession, RequestSettings
from mutiai.api.errors import ApiError, ErrorEnvelope
from mutiai.models import BrowserSession, User
from mutiai.models.base import utc_now
from mutiai.security import (
    create_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=1, max_length=1_024)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    username: str
    display_name: str


class LoginResponse(BaseModel):
    user: UserResponse


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=100)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=1_024)
    new_password: SecretStr = Field(min_length=8, max_length=128)


router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={
        401: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    },
)
def login(
    payload: LoginRequest,
    response: Response,
    session: DbSession,
    settings: RequestSettings,
) -> LoginResponse:
    user = session.scalar(select(User).where(User.username == payload.username))
    password = payload.password.get_secret_value()
    password_is_valid = verify_password(
        password,
        user.password_hash if user is not None else None,
    )
    if user is None or not user.is_active or not password_is_valid:
        raise ApiError(
            401,
            "AUTH_INVALID_CREDENTIALS",
            "The username or password is invalid.",
        )

    token = create_session_token()
    now = utc_now()
    browser_session = BrowserSession(
        user_id=user.user_id,
        token_hash=hash_session_token(token),
        created_at=now,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
    )
    session.add(browser_session)
    session.commit()

    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        path="/",
    )
    return LoginResponse(user=UserResponse.model_validate(user))


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    session: DbSession,
    settings: RequestSettings,
) -> Response:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        browser_session = session.scalar(
            select(BrowserSession).where(
                BrowserSession.token_hash == hash_session_token(token),
                BrowserSession.revoked_at.is_(None),
            )
        )
        if browser_session is not None:
            browser_session.revoked_at = utc_now()
            session.commit()

    response = Response(status_code=204)
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        samesite="lax",
    )
    return response


@router.get(
    "/me",
    response_model=UserResponse,
    responses={401: {"model": ErrorEnvelope}},
)
def me(user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(user)


@router.patch(
    "/me",
    response_model=UserResponse,
    responses={
        401: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    },
)
def update_me(
    payload: UpdateUserRequest,
    session: DbSession,
    user: CurrentUser,
) -> UserResponse:
    user.display_name = payload.display_name
    session.commit()
    session.refresh(user)
    return UserResponse.model_validate(user)


@router.post(
    "/password",
    status_code=204,
    responses={
        400: {"model": ErrorEnvelope},
        401: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    },
)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    session: DbSession,
    settings: RequestSettings,
    user: CurrentUser,
) -> Response:
    current_password = payload.current_password.get_secret_value()
    if not verify_password(current_password, user.password_hash):
        raise ApiError(
            400,
            "AUTH_CURRENT_PASSWORD_INVALID",
            "The current password is invalid.",
        )

    new_password = payload.new_password.get_secret_value()
    if verify_password(new_password, user.password_hash):
        raise ApiError(
            409,
            "AUTH_NEW_PASSWORD_MUST_DIFFER",
            "The new password must differ from the current password.",
        )

    token = request.cookies[settings.session_cookie_name]
    now = utc_now()
    user.password_hash = hash_password(new_password)
    session.execute(
        update(BrowserSession)
        .where(
            BrowserSession.user_id == user.user_id,
            BrowserSession.token_hash != hash_session_token(token),
            BrowserSession.revoked_at.is_(None),
            BrowserSession.expires_at > now,
        )
        .values(revoked_at=now)
    )
    session.commit()
    return Response(status_code=204)
