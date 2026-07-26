"""Runtime feasibility API responses with backend-owned user messages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from mutiai.api.schemas.organizations import as_utc
from mutiai.domain import FeasibilityFinding
from mutiai.models import FeasibilityCheck, FeasibilityOutcome

_ZH_REASONS = {
    "WORKLOAD_REQUIREMENT_CONFLICT": "岗位或任务包含互相冲突的运行环境要求。",
    "RUNTIME_BINDING_UNAVAILABLE": "岗位绑定的 Runtime 不存在或已停用。",
    "CAPABILITY_PROFILE_UNTRUSTED": "Runtime 能力档案不可信，不能据此启动工作。",
    "OS_CAPABILITY_UNKNOWN": "Runtime 没有声明操作系统能力。",
    "OS_CAPABILITY_MISMATCH": "任务要求的操作系统与 Runtime 不匹配。",
    "GUI_CAPABILITY_UNKNOWN": "Runtime 没有声明图形界面能力。",
    "GUI_UNAVAILABLE": "任务需要图形界面，但 Runtime 是无界面环境。",
    "GPU_CAPABILITY_UNKNOWN": "Runtime 没有声明 GPU 能力。",
    "GPU_UNAVAILABLE": "任务需要 GPU，但 Runtime 没有可用 GPU。",
    "GPU_MEMORY_UNKNOWN": "Runtime 没有声明 GPU 显存。",
    "GPU_MEMORY_INSUFFICIENT": "Runtime 的 GPU 显存不足。",
    "CPU_CAPACITY_UNKNOWN": "Runtime 没有声明 CPU 负载能力。",
    "CPU_CAPACITY_INSUFFICIENT": "Runtime 的 CPU 负载能力不足。",
    "MEMORY_CAPACITY_UNKNOWN": "Runtime 没有声明可用内存。",
    "MEMORY_CAPACITY_INSUFFICIENT": "Runtime 的可用内存不足。",
    "REQUIRED_TOOL_UNAVAILABLE": "Runtime 缺少任务要求的工具。",
    "TOOL_INVENTORY_UNKNOWN": "Runtime 的工具清单不完整。",
    "NETWORK_CAPABILITY_UNKNOWN": "Runtime 没有声明网络能力。",
    "NETWORK_UNAVAILABLE": "任务需要网络，但当前 Runtime 不允许联网。",
    "REQUIRED_SERVICE_UNAVAILABLE": "Runtime 无法使用任务要求的外部服务。",
    "SERVICE_INVENTORY_UNKNOWN": "Runtime 的外部服务清单不完整。",
    "REQUIRED_HARDWARE_UNAVAILABLE": "Runtime 缺少任务要求的硬件。",
    "HARDWARE_INVENTORY_UNKNOWN": "Runtime 的硬件清单不完整。",
    "PROPRIETARY_SOFTWARE_UNAVAILABLE": "Runtime 缺少任务要求的专有软件。",
    "PROPRIETARY_SOFTWARE_INVENTORY_UNKNOWN": "Runtime 的专有软件清单不完整。",
    "INPUT_MEDIA_UNSUPPORTED": "Runtime 不支持要求的输入媒体格式。",
    "OUTPUT_MEDIA_UNSUPPORTED": "Runtime 不支持要求的输出媒体格式。",
    "MEDIA_CAPABILITY_UNKNOWN": "Runtime 的媒体格式能力不明确。",
    "DURATION_LIMIT_UNKNOWN": "Runtime 没有声明最长工作时限。",
    "DURATION_LIMIT_EXCEEDED": "任务预计时长超过 Runtime 限制。",
    "INPUT_SIZE_LIMIT_UNKNOWN": "Runtime 没有声明最大输入大小。",
    "INPUT_SIZE_LIMIT_EXCEEDED": "任务输入大小超过 Runtime 限制。",
}

_ZH_ALTERNATIVES = {
    "clarify_workload": "明确任务需要的操作系统和资源条件。",
    "declare_capabilities": "补充可信的 Runtime 能力档案。",
    "use_configured_binding": "选择已配置并启用的 Runtime binding。",
    "use_compatible_binding": "改用操作系统兼容的 Runtime binding。",
    "use_linux_native_tool": "改用可在 Linux 上运行的工具或流程。",
    "use_cross_platform_tool": "改用跨平台工具。",
    "use_headless_tool": "改用支持无界面运行的工具。",
    "manual_handling": "将此步骤交由具备相应环境的人工工作站处理。",
    "use_gpu_binding": "改用明确配备合适 GPU 的 Runtime binding。",
    "use_external_service": "改用具备相应资源的外部服务。",
    "reduce_workload": "缩小输入规模、时长或计算量。",
    "use_capable_binding": "改用能力档案满足要求的 Runtime binding。",
    "use_preconfigured_binding": "改用已预装所需工具的软件环境。",
    "use_network_enabled_binding": "改用允许访问所需网络的 Runtime binding。",
    "convert_input": "先将输入转换为 Runtime 支持的格式。",
    "change_output_format": "改用 Runtime 支持的输出格式。",
}


class FeasibilityFindingResponse(BaseModel):
    reason_code: str
    role_key: str
    binding_key: str
    capability: str
    required: Any | None
    actual: Any | None
    alternative_codes: tuple[str, ...]
    message: str
    alternatives: tuple[str, ...]

    @classmethod
    def from_finding(
        cls,
        finding: FeasibilityFinding,
        *,
        locale: str,
    ) -> FeasibilityFindingResponse:
        if locale == "zh-CN":
            message = _ZH_REASONS.get(
                finding.reason_code,
                "Runtime 能力不满足任务要求。",
            )
            alternatives = tuple(
                _ZH_ALTERNATIVES.get(code, "调整任务或 Runtime 配置。")
                for code in finding.alternative_codes
            )
        else:
            message = finding.reason_code.replace("_", " ").capitalize() + "."
            alternatives = tuple(
                code.replace("_", " ").capitalize() + "."
                for code in finding.alternative_codes
            )
        return cls(
            **finding.model_dump(),
            message=message,
            alternatives=alternatives,
        )


class FeasibilityCheckResponse(BaseModel):
    feasibility_check_id: str
    target_type: str
    target_id: str
    phase: str
    validator_version: str
    input_hash: str
    requirements: list[dict]
    profile_revisions: list[dict]
    outcome: FeasibilityOutcome
    findings: list[FeasibilityFindingResponse]
    created_at: datetime

    @classmethod
    def from_record(
        cls,
        check: FeasibilityCheck,
        *,
        locale: str,
    ) -> FeasibilityCheckResponse:
        findings = [
            FeasibilityFindingResponse.from_finding(
                FeasibilityFinding.model_validate(payload),
                locale=locale,
            )
            for payload in check.findings_payload
        ]
        return cls(
            feasibility_check_id=check.feasibility_check_id,
            target_type=check.target_type,
            target_id=check.target_id,
            phase=check.phase,
            validator_version=check.validator_version,
            input_hash=check.input_hash,
            requirements=check.requirements_payload,
            profile_revisions=check.profile_revisions_payload,
            outcome=FeasibilityOutcome(check.outcome),
            findings=findings,
            created_at=as_utc(check.created_at),
        )
