"""Deterministic Runtime feasibility checks for organizations and Tasks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.config import Settings
from mutiai.domain import (
    FeasibilityFinding,
    OrganizationSpec,
    RuntimeCapabilityProfileSpec,
    WorkloadRequirements,
)
from mutiai.models import (
    FeasibilityCheck,
    FeasibilityOutcome,
    RuntimeBinding,
    RuntimeSecurityMode,
)
from mutiai.services.runtime_bindings import RuntimeBindingService

VALIDATOR_VERSION = "1.0"
_CAPACITY_RANK = {"unknown": 0, "light": 1, "standard": 2, "heavy": 3}

_WINDOWS_MARKERS = (
    "windows",
    "win32",
    "powershell",
    "wpf",
    "winui",
    "windows registry",
    "注册表",
    "com automation",
)
_GUI_MARKERS = (
    "windows gui",
    "desktop gui",
    "desktop application",
    "图形界面",
    "桌面界面",
    "鼠标点击",
)
_HEAVY_MARKERS = (
    "video editing",
    "edit video",
    "edit and render",
    "render long video",
    "video render",
    "video transcoding",
    "3d rendering",
    "model training",
    "train a model",
    "视频剪辑",
    "剪辑视频",
    "视频渲染",
    "视频转码",
    "三维渲染",
    "3d 渲染",
    "模型训练",
    "训练模型",
)
_GPU_MARKERS = (
    "cuda",
    "gpu required",
    "requires gpu",
    "stable diffusion",
    "3d rendering",
    "model training",
    "必须使用 gpu",
    "需要 gpu",
    "三维渲染",
    "3d 渲染",
    "模型训练",
)


class FeasibilityGateError(RuntimeError):
    """A product transition failed its deterministic feasibility gate."""

    reason = "runtime_feasibility_blocked"

    def __init__(self, check: FeasibilityCheck) -> None:
        self.check_id = check.feasibility_check_id
        self.outcome = FeasibilityOutcome(check.outcome)
        super().__init__(
            f"feasibility check '{self.check_id}' returned '{self.outcome.value}'"
        )


@dataclass(frozen=True, slots=True)
class _RoleWorkload:
    role_key: str
    binding_key: str
    requirements: WorkloadRequirements
    requirement_conflict: bool = False


class FeasibilityService:
    """Evaluate, persist, and enforce provider-neutral Runtime requirements."""

    def __init__(
        self,
        settings: Settings,
        runtime_bindings: RuntimeBindingService,
    ) -> None:
        self.settings = settings
        self.runtime_bindings = runtime_bindings

    def evaluate_organization_spec(
        self,
        session: Session,
        *,
        owner_user_id: str,
        spec: OrganizationSpec,
        target_id: str,
        phase: str,
    ) -> FeasibilityCheck:
        workloads = []
        for role in spec.roles:
            inferred = infer_policy_requirements(
                f"{role.name}\n{role.responsibility}"
            )
            merged, conflict = merge_requirements(
                role.capability_requirements,
                inferred,
            )
            workloads.append(
                _RoleWorkload(
                    role_key=role.role_key,
                    binding_key=role.runtime_binding_key,
                    requirements=merged,
                    requirement_conflict=conflict,
                )
            )
        return self._evaluate(
            session,
            owner_user_id=owner_user_id,
            target_type="organization_version",
            target_id=target_id,
            phase=phase,
            workloads=workloads,
        )

    def evaluate_task_request(
        self,
        session: Session,
        *,
        owner_user_id: str,
        spec: OrganizationSpec,
        request_text: str,
        explicit_requirements: WorkloadRequirements,
        target_id: str,
        phase: str,
        role_key: str | None = None,
    ) -> FeasibilityCheck:
        task_inferred = infer_policy_requirements(request_text)
        task_requirements, task_conflict = merge_requirements(
            explicit_requirements,
            task_inferred,
        )
        workloads = []
        for role in spec.roles:
            if role_key is not None and role.role_key != role_key:
                continue
            role_inferred = infer_policy_requirements(
                f"{role.name}\n{role.responsibility}"
            )
            role_requirements, role_conflict = merge_requirements(
                role.capability_requirements,
                role_inferred,
            )
            merged, merge_conflict = merge_requirements(
                role_requirements,
                task_requirements,
            )
            workloads.append(
                _RoleWorkload(
                    role_key=role.role_key,
                    binding_key=role.runtime_binding_key,
                    requirements=merged,
                    requirement_conflict=(
                        task_conflict or role_conflict or merge_conflict
                    ),
                )
            )
        return self._evaluate(
            session,
            owner_user_id=owner_user_id,
            target_type=(
                "task"
                if phase in {"task_submission", "runtime_start"}
                else "task_request"
            ),
            target_id=target_id,
            phase=phase,
            workloads=workloads,
        )

    @staticmethod
    def require_feasible(check: FeasibilityCheck) -> None:
        if check.outcome != FeasibilityOutcome.FEASIBLE:
            raise FeasibilityGateError(check)

    def _evaluate(
        self,
        session: Session,
        *,
        owner_user_id: str,
        target_type: str,
        target_id: str,
        phase: str,
        workloads: list[_RoleWorkload],
    ) -> FeasibilityCheck:
        findings: list[FeasibilityFinding] = []
        requirements_payload = []
        profiles_payload = []

        for workload in workloads:
            requirements_payload.append(
                {
                    "role_key": workload.role_key,
                    "binding_key": workload.binding_key,
                    "requirements": workload.requirements.model_dump(mode="json"),
                }
            )
            if workload.requirement_conflict:
                findings.append(
                    self._finding(
                        "WORKLOAD_REQUIREMENT_CONFLICT",
                        workload,
                        "os_family",
                        required=list(workload.requirements.os_families),
                        actual="conflicting declarations",
                        alternatives=("clarify_workload",),
                    )
                )

            binding = session.scalar(
                select(RuntimeBinding).where(
                    RuntimeBinding.owner_user_id == owner_user_id,
                    RuntimeBinding.binding_key == workload.binding_key,
                )
            )
            if binding is None and workload.binding_key == (
                self.settings.runtime_default_binding_key
            ):
                binding = self.runtime_bindings.ensure_default(
                    session,
                    owner_user_id=owner_user_id,
                )
            if binding is None or not binding.is_active:
                findings.append(
                    self._finding(
                        "RUNTIME_BINDING_UNAVAILABLE",
                        workload,
                        "runtime_binding",
                        required=workload.binding_key,
                        actual=None,
                        alternatives=("use_configured_binding",),
                    )
                )
                continue

            profile_record = self.runtime_bindings.ensure_profile(
                session,
                binding=binding,
                owner_user_id=owner_user_id,
            )
            profile = RuntimeCapabilityProfileSpec.model_validate(
                profile_record.profile_payload
            )
            profiles_payload.append(
                {
                    "role_key": workload.role_key,
                    "binding_key": binding.binding_key,
                    "runtime_binding_id": binding.runtime_binding_id,
                    "capability_profile_id": (
                        profile_record.capability_profile_id
                    ),
                    "revision": profile_record.revision,
                }
            )
            if not profile_record.trusted:
                findings.append(
                    self._finding(
                        "CAPABILITY_PROFILE_UNTRUSTED",
                        workload,
                        "capability_profile",
                        required="trusted profile",
                        actual="untrusted",
                        alternatives=("declare_capabilities",),
                    )
                )
            findings.extend(
                self._validate_profile(
                    workload,
                    binding=binding,
                    profile=profile,
                )
            )

        outcome = self._outcome(findings)
        canonical = {
            "target_type": target_type,
            "target_id": target_id,
            "phase": phase,
            "validator_version": VALIDATOR_VERSION,
            "requirements": requirements_payload,
            "profiles": profiles_payload,
        }
        input_hash = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        check = FeasibilityCheck(
            owner_user_id=owner_user_id,
            target_type=target_type,
            target_id=target_id,
            phase=phase,
            validator_version=VALIDATOR_VERSION,
            input_hash=input_hash,
            requirements_payload=requirements_payload,
            profile_revisions_payload=profiles_payload,
            outcome=outcome,
            findings_payload=[item.model_dump(mode="json") for item in findings],
        )
        session.add(check)
        session.commit()
        session.refresh(check)
        return check

    def _validate_profile(
        self,
        workload: _RoleWorkload,
        *,
        binding: RuntimeBinding,
        profile: RuntimeCapabilityProfileSpec,
    ) -> list[FeasibilityFinding]:
        requirements = workload.requirements
        findings: list[FeasibilityFinding] = []

        if requirements.os_families:
            if profile.os_family == "unknown":
                findings.append(
                    self._unknown(
                        "OS_CAPABILITY_UNKNOWN",
                        workload,
                        "os_family",
                        list(requirements.os_families),
                    )
                )
            elif profile.os_family not in requirements.os_families:
                alternatives = (
                    "use_linux_native_tool",
                    "use_compatible_binding",
                )
                findings.append(
                    self._finding(
                        "OS_CAPABILITY_MISMATCH",
                        workload,
                        "os_family",
                        required=list(requirements.os_families),
                        actual=profile.os_family,
                        alternatives=alternatives,
                    )
                )

        if requirements.requires_gui:
            if profile.headless is None:
                findings.append(
                    self._unknown(
                        "GUI_CAPABILITY_UNKNOWN",
                        workload,
                        "headless",
                        False,
                    )
                )
            elif profile.headless:
                findings.append(
                    self._finding(
                        "GUI_UNAVAILABLE",
                        workload,
                        "headless",
                        required=False,
                        actual=True,
                        alternatives=("use_headless_tool", "manual_handling"),
                    )
                )

        if requirements.requires_gpu:
            if profile.gpu_available is None:
                findings.append(
                    self._unknown(
                        "GPU_CAPABILITY_UNKNOWN",
                        workload,
                        "gpu_available",
                        True,
                    )
                )
            elif not profile.gpu_available:
                findings.append(
                    self._finding(
                        "GPU_UNAVAILABLE",
                        workload,
                        "gpu_available",
                        required=True,
                        actual=False,
                        alternatives=("use_gpu_binding", "use_external_service"),
                    )
                )
            elif requirements.min_gpu_memory_mb is not None:
                if profile.gpu_memory_mb is None:
                    findings.append(
                        self._unknown(
                            "GPU_MEMORY_UNKNOWN",
                            workload,
                            "gpu_memory_mb",
                            requirements.min_gpu_memory_mb,
                        )
                    )
                elif profile.gpu_memory_mb < requirements.min_gpu_memory_mb:
                    findings.append(
                        self._finding(
                            "GPU_MEMORY_INSUFFICIENT",
                            workload,
                            "gpu_memory_mb",
                            required=requirements.min_gpu_memory_mb,
                            actual=profile.gpu_memory_mb,
                            alternatives=("use_gpu_binding", "reduce_workload"),
                        )
                    )

        required_cpu = requirements.min_cpu_capacity
        actual_cpu = profile.cpu_capacity_class
        if actual_cpu == "unknown":
            findings.append(
                self._unknown(
                    "CPU_CAPACITY_UNKNOWN",
                    workload,
                    "cpu_capacity_class",
                    required_cpu,
                )
            )
        elif _CAPACITY_RANK[actual_cpu] < _CAPACITY_RANK[required_cpu]:
            findings.append(
                self._finding(
                    "CPU_CAPACITY_INSUFFICIENT",
                    workload,
                    "cpu_capacity_class",
                    required=required_cpu,
                    actual=actual_cpu,
                    alternatives=("reduce_workload", "use_external_service"),
                )
            )

        if requirements.min_memory_mb is not None:
            if profile.memory_mb is None:
                findings.append(
                    self._unknown(
                        "MEMORY_CAPACITY_UNKNOWN",
                        workload,
                        "memory_mb",
                        requirements.min_memory_mb,
                    )
                )
            elif profile.memory_mb < requirements.min_memory_mb:
                findings.append(
                    self._finding(
                        "MEMORY_CAPACITY_INSUFFICIENT",
                        workload,
                        "memory_mb",
                        required=requirements.min_memory_mb,
                        actual=profile.memory_mb,
                        alternatives=("reduce_workload", "use_capable_binding"),
                    )
                )

        findings.extend(
            self._validate_inventory(
                workload,
                capability="installed_tools",
                required=requirements.required_tools,
                available=profile.installed_tools,
                complete=profile.tool_inventory_complete,
                blocked_code="REQUIRED_TOOL_UNAVAILABLE",
                unknown_code="TOOL_INVENTORY_UNKNOWN",
                alternatives=("use_preconfigured_binding",),
            )
        )

        if requirements.requires_network:
            network_access = profile.network_access
            if binding.security_mode == RuntimeSecurityMode.WORKSPACE_RESTRICTED:
                network_access = False
            if network_access is None:
                findings.append(
                    self._unknown(
                        "NETWORK_CAPABILITY_UNKNOWN",
                        workload,
                        "network_access",
                        True,
                    )
                )
            elif not network_access:
                findings.append(
                    self._finding(
                        "NETWORK_UNAVAILABLE",
                        workload,
                        "network_access",
                        required=True,
                        actual=False,
                        alternatives=("use_network_enabled_binding",),
                    )
                )

        for values in (
            (
                "external_services",
                requirements.required_external_services,
                profile.external_services,
                profile.external_service_inventory_complete,
                "REQUIRED_SERVICE_UNAVAILABLE",
                "SERVICE_INVENTORY_UNKNOWN",
                ("use_external_service", "use_preconfigured_binding"),
            ),
            (
                "attached_hardware",
                requirements.required_hardware,
                profile.attached_hardware,
                profile.hardware_inventory_complete,
                "REQUIRED_HARDWARE_UNAVAILABLE",
                "HARDWARE_INVENTORY_UNKNOWN",
                ("manual_handling", "use_capable_binding"),
            ),
            (
                "proprietary_software",
                requirements.required_proprietary_software,
                profile.proprietary_software,
                profile.proprietary_software_inventory_complete,
                "PROPRIETARY_SOFTWARE_UNAVAILABLE",
                "PROPRIETARY_SOFTWARE_INVENTORY_UNKNOWN",
                ("use_cross_platform_tool", "manual_handling"),
            ),
            (
                "supported_input_media_types",
                requirements.input_media_types,
                profile.supported_input_media_types,
                profile.media_inventory_complete,
                "INPUT_MEDIA_UNSUPPORTED",
                "MEDIA_CAPABILITY_UNKNOWN",
                ("convert_input", "use_capable_binding"),
            ),
            (
                "supported_output_media_types",
                requirements.output_media_types,
                profile.supported_output_media_types,
                profile.media_inventory_complete,
                "OUTPUT_MEDIA_UNSUPPORTED",
                "MEDIA_CAPABILITY_UNKNOWN",
                ("change_output_format", "use_capable_binding"),
            ),
        ):
            findings.extend(
                self._validate_inventory(
                    workload,
                    capability=values[0],
                    required=values[1],
                    available=values[2],
                    complete=values[3],
                    blocked_code=values[4],
                    unknown_code=values[5],
                    alternatives=values[6],
                )
            )

        if requirements.estimated_duration_seconds is not None:
            findings.extend(
                self._validate_limit(
                    workload,
                    capability="max_duration_seconds",
                    required=requirements.estimated_duration_seconds,
                    actual=profile.max_duration_seconds,
                    unknown_code="DURATION_LIMIT_UNKNOWN",
                    blocked_code="DURATION_LIMIT_EXCEEDED",
                )
            )
        if requirements.estimated_input_size_bytes is not None:
            findings.extend(
                self._validate_limit(
                    workload,
                    capability="max_input_size_bytes",
                    required=requirements.estimated_input_size_bytes,
                    actual=profile.max_input_size_bytes,
                    unknown_code="INPUT_SIZE_LIMIT_UNKNOWN",
                    blocked_code="INPUT_SIZE_LIMIT_EXCEEDED",
                )
            )
        return findings

    def _validate_inventory(
        self,
        workload: _RoleWorkload,
        *,
        capability: str,
        required: tuple[str, ...],
        available: tuple[str, ...],
        complete: bool,
        blocked_code: str,
        unknown_code: str,
        alternatives: tuple[str, ...],
    ) -> list[FeasibilityFinding]:
        available_by_key = {item.casefold(): item for item in available}
        missing = [item for item in required if item.casefold() not in available_by_key]
        if not missing:
            return []
        if complete:
            return [
                self._finding(
                    blocked_code,
                    workload,
                    capability,
                    required=missing,
                    actual=list(available),
                    alternatives=alternatives,
                )
            ]
        return [
            self._unknown(
                unknown_code,
                workload,
                capability,
                missing,
            )
        ]

    def _validate_limit(
        self,
        workload: _RoleWorkload,
        *,
        capability: str,
        required: int,
        actual: int | None,
        unknown_code: str,
        blocked_code: str,
    ) -> list[FeasibilityFinding]:
        if actual is None:
            return [self._unknown(unknown_code, workload, capability, required)]
        if required <= actual:
            return []
        return [
            self._finding(
                blocked_code,
                workload,
                capability,
                required=required,
                actual=actual,
                alternatives=("reduce_workload", "use_capable_binding"),
            )
        ]

    @staticmethod
    def _finding(
        reason_code: str,
        workload: _RoleWorkload,
        capability: str,
        *,
        required: Any,
        actual: Any,
        alternatives: tuple[str, ...],
    ) -> FeasibilityFinding:
        return FeasibilityFinding(
            reason_code=reason_code,
            role_key=workload.role_key,
            binding_key=workload.binding_key,
            capability=capability,
            required=required,
            actual=actual,
            alternative_codes=alternatives,
        )

    def _unknown(
        self,
        reason_code: str,
        workload: _RoleWorkload,
        capability: str,
        required: Any,
    ) -> FeasibilityFinding:
        return self._finding(
            reason_code,
            workload,
            capability,
            required=required,
            actual=None,
            alternatives=("declare_capabilities", "use_capable_binding"),
        )

    @staticmethod
    def _outcome(findings: list[FeasibilityFinding]) -> FeasibilityOutcome:
        if not findings:
            return FeasibilityOutcome.FEASIBLE
        if any(
            item.reason_code.endswith(
                (
                    "UNKNOWN",
                    "UNTRUSTED",
                )
            )
            for item in findings
        ):
            known_mismatch = any(
                not item.reason_code.endswith(("UNKNOWN", "UNTRUSTED"))
                for item in findings
            )
            if not known_mismatch:
                return FeasibilityOutcome.CAPABILITY_UNKNOWN
        return FeasibilityOutcome.BLOCKED


def infer_policy_requirements(text: str) -> WorkloadRequirements:
    """Apply the V1 fail-closed catalog to known risky workload language."""

    normalized = text.casefold()
    windows_only = any(marker in normalized for marker in _WINDOWS_MARKERS)
    requires_gui = any(marker in normalized for marker in _GUI_MARKERS)
    resource_intensive = any(marker in normalized for marker in _HEAVY_MARKERS)
    requires_gpu = any(marker in normalized for marker in _GPU_MARKERS)
    return WorkloadRequirements(
        os_families=("windows",) if windows_only else (),
        requires_gui=requires_gui,
        requires_gpu=requires_gpu,
        min_cpu_capacity="heavy" if resource_intensive else "light",
        resource_intensive=resource_intensive,
    )


def merge_requirements(
    first: WorkloadRequirements,
    second: WorkloadRequirements,
) -> tuple[WorkloadRequirements, bool]:
    """Merge hard requirements and report contradictory OS declarations."""

    first_os = set(first.os_families)
    second_os = set(second.os_families)
    conflict = bool(first_os and second_os and not (first_os & second_os))
    if first_os and second_os:
        os_families = tuple(sorted(first_os & second_os))
    else:
        os_families = first.os_families or second.os_families

    def union(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
        values: dict[str, str] = {}
        for item in (*left, *right):
            values.setdefault(item.casefold(), item)
        return tuple(values.values())

    min_cpu = max(
        (first.min_cpu_capacity, second.min_cpu_capacity),
        key=lambda value: _CAPACITY_RANK[value],
    )
    resource_intensive = first.resource_intensive or second.resource_intensive
    if resource_intensive:
        min_cpu = "heavy"

    def maximum(left: int | None, right: int | None) -> int | None:
        values = [value for value in (left, right) if value is not None]
        return max(values) if values else None

    return (
        WorkloadRequirements(
            os_families=os_families,
            requires_gui=first.requires_gui or second.requires_gui,
            requires_gpu=first.requires_gpu or second.requires_gpu,
            min_gpu_memory_mb=maximum(
                first.min_gpu_memory_mb,
                second.min_gpu_memory_mb,
            ),
            min_cpu_capacity=min_cpu,
            min_memory_mb=maximum(first.min_memory_mb, second.min_memory_mb),
            required_tools=union(first.required_tools, second.required_tools),
            requires_network=first.requires_network or second.requires_network,
            required_external_services=union(
                first.required_external_services,
                second.required_external_services,
            ),
            required_hardware=union(
                first.required_hardware,
                second.required_hardware,
            ),
            required_proprietary_software=union(
                first.required_proprietary_software,
                second.required_proprietary_software,
            ),
            input_media_types=union(
                first.input_media_types,
                second.input_media_types,
            ),
            output_media_types=union(
                first.output_media_types,
                second.output_media_types,
            ),
            estimated_duration_seconds=maximum(
                first.estimated_duration_seconds,
                second.estimated_duration_seconds,
            ),
            estimated_input_size_bytes=maximum(
                first.estimated_input_size_bytes,
                second.estimated_input_size_bytes,
            ),
            resource_intensive=resource_intensive,
        ),
        conflict,
    )
