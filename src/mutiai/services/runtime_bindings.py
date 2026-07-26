"""Resolve product-owned role bindings into Runtime execution policy."""

from __future__ import annotations

import platform
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mutiai.config import Settings
from mutiai.domain import OrganizationSpec, RuntimeCapabilityProfileSpec
from mutiai.models import (
    RuntimeBinding,
    RuntimeCapabilityProfile,
    RuntimeSecurityMode,
)
from mutiai.runtime import RuntimeExecutionConfig


class RuntimeBindingResolutionError(RuntimeError):
    """A role cannot be mapped to an active binding for the selected Runtime."""

    reason = "runtime_binding_invalid"


@dataclass(frozen=True, slots=True)
class RuntimeBindingInput:
    """Mutable API input kept separate from the ORM record."""

    binding_key: str
    provider: str
    model: str | None
    reasoning_effort: str | None
    security_mode: RuntimeSecurityMode
    capability_profile: RuntimeCapabilityProfileSpec | None = None


class RuntimeBindingService:
    """Own binding records and compile them into Runtime-neutral settings."""

    def __init__(self, settings: Settings, *, runtime_provider: str) -> None:
        self.settings = settings
        self.runtime_provider = runtime_provider

    def list_for_owner(
        self,
        session: Session,
        *,
        owner_user_id: str,
    ) -> list[RuntimeBinding]:
        return list(
            session.scalars(
                select(RuntimeBinding)
                .where(RuntimeBinding.owner_user_id == owner_user_id)
                .order_by(RuntimeBinding.binding_key)
            ).all()
        )

    def upsert(
        self,
        session: Session,
        *,
        owner_user_id: str,
        data: RuntimeBindingInput,
    ) -> RuntimeBinding:
        self._require_allowed_security_mode(data.security_mode)
        binding = session.scalar(
            select(RuntimeBinding).where(
                RuntimeBinding.owner_user_id == owner_user_id,
                RuntimeBinding.binding_key == data.binding_key,
            )
        )
        if binding is None:
            binding = RuntimeBinding(
                owner_user_id=owner_user_id,
                binding_key=data.binding_key,
                provider=data.provider,
                model=data.model,
                reasoning_effort=data.reasoning_effort,
                security_mode=data.security_mode,
                is_active=True,
            )
            session.add(binding)
            session.flush()
        else:
            binding.provider = data.provider
            binding.model = data.model
            binding.reasoning_effort = data.reasoning_effort
            binding.security_mode = data.security_mode
            binding.is_active = True
        if data.capability_profile is not None:
            self._append_profile_if_changed(
                session,
                binding=binding,
                owner_user_id=owner_user_id,
                profile=data.capability_profile,
                source="user_declared",
            )
        else:
            self.ensure_profile(
                session,
                binding=binding,
                owner_user_id=owner_user_id,
            )
        session.commit()
        session.refresh(binding)
        return binding

    def ensure_default(
        self,
        session: Session,
        *,
        owner_user_id: str,
    ) -> RuntimeBinding:
        """Create the local default binding lazily on first Runtime use."""

        key = self.settings.runtime_default_binding_key
        binding = session.scalar(
            select(RuntimeBinding).where(
                RuntimeBinding.owner_user_id == owner_user_id,
                RuntimeBinding.binding_key == key,
            )
        )
        if binding is not None:
            self.ensure_profile(
                session,
                binding=binding,
                owner_user_id=owner_user_id,
            )
            return binding
        binding = RuntimeBinding(
            owner_user_id=owner_user_id,
            binding_key=key,
            provider=self.runtime_provider,
            model=self.settings.codex_model,
            reasoning_effort=self.settings.codex_reasoning_effort,
            security_mode=RuntimeSecurityMode(self.settings.runtime_security_mode),
            is_active=True,
        )
        session.add(binding)
        session.flush()
        self.ensure_profile(
            session,
            binding=binding,
            owner_user_id=owner_user_id,
        )
        return binding

    def current_profile(
        self,
        session: Session,
        *,
        binding: RuntimeBinding,
    ) -> RuntimeCapabilityProfile | None:
        return session.scalar(
            select(RuntimeCapabilityProfile)
            .where(
                RuntimeCapabilityProfile.runtime_binding_id
                == binding.runtime_binding_id
            )
            .order_by(RuntimeCapabilityProfile.revision.desc())
        )

    def ensure_profile(
        self,
        session: Session,
        *,
        binding: RuntimeBinding,
        owner_user_id: str,
    ) -> RuntimeCapabilityProfile:
        current = self.current_profile(session, binding=binding)
        if current is not None:
            return current
        return self._append_profile_if_changed(
            session,
            binding=binding,
            owner_user_id=owner_user_id,
            profile=self._default_profile(binding),
            source="runtime_default",
        )

    def _append_profile_if_changed(
        self,
        session: Session,
        *,
        binding: RuntimeBinding,
        owner_user_id: str,
        profile: RuntimeCapabilityProfileSpec,
        source: str,
    ) -> RuntimeCapabilityProfile:
        current = self.current_profile(session, binding=binding)
        payload = profile.model_dump(mode="json")
        if current is not None and current.profile_payload == payload:
            return current
        latest_revision = session.scalar(
            select(func.max(RuntimeCapabilityProfile.revision)).where(
                RuntimeCapabilityProfile.runtime_binding_id
                == binding.runtime_binding_id
            )
        )
        record = RuntimeCapabilityProfile(
            owner_user_id=owner_user_id,
            runtime_binding_id=binding.runtime_binding_id,
            revision=(latest_revision or 0) + 1,
            profile_payload=payload,
            source=source,
            trusted=True,
            observed_at=None,
        )
        session.add(record)
        session.flush()
        return record

    @staticmethod
    def _default_profile(binding: RuntimeBinding) -> RuntimeCapabilityProfileSpec:
        system = platform.system().casefold()
        os_family = (
            "windows"
            if system == "windows"
            else "macos"
            if system == "darwin"
            else "linux"
            if system == "linux"
            else "unknown"
        )
        return RuntimeCapabilityProfileSpec(
            os_family=os_family,
            os_version=platform.release() or None,
            architecture=platform.machine() or None,
            # Managed Codex work is headless by default even on a Windows dev host.
            headless=True,
            cpu_capacity_class="standard",
            gpu_available=False,
            network_access=(
                binding.security_mode == RuntimeSecurityMode.DEMO_FULL_ACCESS
            ),
        )

    def resolve_for_role(
        self,
        session: Session,
        *,
        owner_user_id: str,
        spec: OrganizationSpec,
        role_key: str,
    ) -> tuple[RuntimeBinding, RuntimeExecutionConfig]:
        role = next((item for item in spec.roles if item.role_key == role_key), None)
        if role is None:
            raise RuntimeBindingResolutionError(
                f"organization spec has no role '{role_key}'"
            )
        binding = session.scalar(
            select(RuntimeBinding).where(
                RuntimeBinding.owner_user_id == owner_user_id,
                RuntimeBinding.binding_key == role.runtime_binding_key,
            )
        )
        if binding is None and role.runtime_binding_key == (
            self.settings.runtime_default_binding_key
        ):
            binding = self.ensure_default(session, owner_user_id=owner_user_id)
        if binding is None:
            raise RuntimeBindingResolutionError(
                f"Runtime binding '{role.runtime_binding_key}' is not configured"
            )
        if not binding.is_active:
            raise RuntimeBindingResolutionError(
                f"Runtime binding '{binding.binding_key}' is inactive"
            )
        if binding.provider != self.runtime_provider:
            raise RuntimeBindingResolutionError(
                f"Runtime binding '{binding.binding_key}' targets provider "
                f"'{binding.provider}', but '{self.runtime_provider}' is active"
            )
        return binding, self.to_execution_config(binding)

    def to_execution_config(
        self,
        binding: RuntimeBinding,
    ) -> RuntimeExecutionConfig:
        security_mode = RuntimeSecurityMode(binding.security_mode)
        self._require_allowed_security_mode(security_mode)
        full_access = security_mode == RuntimeSecurityMode.DEMO_FULL_ACCESS
        return RuntimeExecutionConfig(
            binding_key=binding.binding_key,
            model=binding.model,
            reasoning_effort=binding.reasoning_effort,
            security_mode=security_mode.value,
            approval_policy="never" if full_access else "on-request",
            sandbox_mode=(
                "danger-full-access" if full_access else "workspace-write"
            ),
            network_access=full_access,
        )

    def _require_allowed_security_mode(
        self,
        security_mode: RuntimeSecurityMode,
    ) -> None:
        if security_mode != RuntimeSecurityMode.DEMO_FULL_ACCESS:
            return
        if self.settings.app_env == "production":
            raise RuntimeBindingResolutionError(
                "demo Full Access cannot be used in production"
            )
        if self.settings.app_host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeBindingResolutionError(
                "demo Full Access requires APP_HOST to remain on loopback"
            )
