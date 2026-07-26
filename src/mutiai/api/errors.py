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

DEFAULT_LOCALE = "en-US"
SUPPORTED_LOCALES = frozenset({"en-US", "zh-CN"})

_ZH_MESSAGES = {
    "AUTH_INVALID_CREDENTIALS": "用户名或密码无效。",
    "AUTH_REQUIRED": "需要登录后才能执行此操作。",
    "INVALID_REQUEST": "请求参数无效。",
    "ORGANIZATION_NOT_FOUND": "未找到组织。",
    "ORGANIZATION_VERSION_NOT_FOUND": "未找到组织版本。",
    "ORGANIZATION_VERSION_STALE": "组织已有更新的版本。",
    "ORGANIZATION_VERSION_STATE_CONFLICT": "组织版本当前状态不允许此操作。",
    "ORGANIZATION_NOT_PUBLISHED": "请先发布组织版本，再创建任务。",
    "ORGANIZATION_VERSION_MISSING": "任务关联的组织版本不可用。",
    "ORGANIZATION_HAS_NO_SPECIALISTS": "组织中没有可执行此任务的专业岗位。",
    "TASK_NOT_FOUND": "未找到任务。",
    "TASK_IDEMPOTENCY_CONFLICT": "此幂等键已用于其他请求。",
    "TASK_PLAN_SUPPORTED_SHAPE_REQUIRED": "任务计划必须是当前支持的线性或纯并行结构。",
    "TASK_PLAN_UNKNOWN_ROLE": "任务计划引用了不存在的岗位。",
    "TASK_PLAN_ROLE_REUSED": "同一任务计划中不能重复使用岗位。",
    "TASK_PLAN_LEAD_REVIEW_REQUIRED": "任务计划最后一步必须由组织负责人执行审核。",
    "TASK_PLAN_LEAD_REVIEW_OUTPUT_FORBIDDEN": "组织负责人审核不能替代专业岗位的交付物。",
    "TASK_PLAN_SPECIALIST_STEP_INVALID": "负责人审核之前的步骤必须由专业岗位执行。",
    "TASK_PLAN_PARALLEL_OUTPUT_REQUIRED": "并行专业岗位必须声明至少一个输出交付物。",
    "TASK_PLAN_PARALLEL_REVIEW_INPUTS_INCOMPLETE": "负责人审核必须完整声明所有专业岗位交付物。",
    "TASK_PLAN_VERSION_CONFLICT": "任务计划版本已包含其他定义。",
    "TASK_NOT_PLANNED": "任务当前尚未完成计划。",
    "TASK_PLAN_NOT_ALLOWED": "当前任务状态不允许提交计划。",
    "TASK_NOT_READY_TO_START": "任务当前尚未准备好启动。",
    "TASK_NOT_RETRYABLE": "只有失败任务可以重试。",
    "TASK_NOT_CANCELLABLE": "只有未结束的任务可以取消。",
    "TASK_CANCELLATION_INCOMPLETE": "任务已请求取消，但部分 Runtime 中断尚未确认。",
    "TASK_EVENT_CURSOR_INVALID": "任务事件游标不可用。",
    "TASK_INPUT_BASE64_INVALID": "任务输入文件内容不是有效的 Base64。",
    "TASK_INPUT_INVALID": "任务输入无效。",
    "ARTIFACT_NOT_FOUND": "未找到交付物。",
    "ARTIFACT_NOT_RELEASED": "交付物尚未发布。",
    "APPROVAL_NOT_FOUND": "未找到审批请求。",
    "APPROVAL_ALREADY_RESOLVED": "审批请求已经使用其他决定处理。",
    "APPROVAL_NOT_ACTIVE": "Runtime 已不再等待此审批。",
    "RUNTIME_PROVIDER_MISMATCH": "Runtime Provider 与当前服务配置不匹配。",
    "RUNTIME_SECURITY_MODE_INVALID": "Runtime 安全模式无效。",
    "RUNTIME_BINDING_INVALID": "Runtime 绑定无效。",
    "RUNTIME_BUDGET_EXCEEDED": "Runtime 预算不足，无法执行此任务。",
    "PROVIDER_RATE_LIMITED": "Runtime Provider 当前受到速率限制，请稍后重试。",
    "INTERNAL_ERROR": "服务器发生未预期的错误。",
    "HTTP_ERROR": "请求无法完成。",
}

_ZH_PREFIX_MESSAGES = (
    ("ARTIFACT_", "交付物校验或交付失败。"),
    ("TASK_PLAN_", "任务计划不符合当前支持的规则。"),
    ("TASK_", "任务操作无法完成。"),
    ("ORGANIZATION_", "组织操作无法完成。"),
    ("RUNTIME_", "Runtime 操作无法完成。"),
    ("APPROVAL_", "审批操作无法完成。"),
)

_ZH_STATUS_MESSAGES = {
    400: "请求无效。",
    401: "请先登录。",
    403: "没有权限执行此操作。",
    404: "未找到请求的资源。",
    409: "当前资源状态不允许此操作。",
    422: "请求参数无效。",
    429: "请求过于频繁，请稍后重试。",
    500: "服务器发生未预期的错误。",
}


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


def resolve_locale(accept_language: str | None) -> str:
    """Resolve the first supported locale from an Accept-Language header."""

    if not accept_language:
        return DEFAULT_LOCALE

    preferences: list[tuple[float, int, str]] = []
    for position, value in enumerate(accept_language.split(",")):
        parts = [part.strip() for part in value.split(";")]
        language = parts[0].lower()
        if not language:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            key, separator, raw_value = parameter.partition("=")
            if key.strip().lower() != "q" or not separator:
                continue
            try:
                quality = float(raw_value)
            except ValueError:
                quality = 0.0
            break
        if quality <= 0:
            continue
        preferences.append((quality, -position, language))

    for _, _, language in sorted(preferences, reverse=True):
        if language == "*":
            return DEFAULT_LOCALE
        if language == "zh" or language.startswith("zh-"):
            return "zh-CN"
        if language == "en" or language.startswith("en-"):
            return "en-US"
    return DEFAULT_LOCALE


def _localized_message(
    *,
    code: str,
    fallback: str,
    locale: str,
    status_code: int,
) -> str:
    if locale == "en-US":
        return fallback
    if code in _ZH_MESSAGES:
        return _ZH_MESSAGES[code]
    for prefix, message in _ZH_PREFIX_MESSAGES:
        if code.startswith(prefix):
            return message
    return _ZH_STATUS_MESSAGES.get(status_code, "请求无法完成。")


def _localized_validation_message(error_type: str, locale: str) -> str:
    if locale == "en-US":
        return ""
    return {
        "missing": "此字段为必填项。",
        "string_too_short": "文本长度不足。",
        "string_too_long": "文本长度超出限制。",
        "too_short": "文本长度不足。",
        "too_long": "文本长度超出限制。",
        "int_parsing": "必须是整数。",
        "float_parsing": "必须是数字。",
        "json_invalid": "JSON 格式无效。",
        "literal_error": "字段值不符合允许的选项。",
        "value_error": "字段值无效。",
    }.get(error_type, "字段值无效。")


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
    locale = resolve_locale(request.headers.get("Accept-Language"))
    envelope = ErrorEnvelope(
        code=code,
        message=_localized_message(
            code=code,
            fallback=message,
            locale=locale,
            status_code=status_code,
        ),
        request_id=_request_id(request),
        details=details,
    )
    response = JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(exclude_none=True),
    )
    response.headers["Content-Language"] = locale
    response.headers["Vary"] = "Accept-Language"
    return response


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
        locale = resolve_locale(request.headers.get("Accept-Language"))
        details = []
        for error in exc.errors():
            item = {
                "location": list(error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            if locale != "en-US":
                item["message"] = _localized_validation_message(
                    error["type"],
                    locale,
                )
            details.append(item)
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
