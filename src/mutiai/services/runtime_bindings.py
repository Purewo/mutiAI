"""Resolve product-owned role bindings into Runtime execution policy."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.config import Settings
from mutiai.domain import OrganizationSpec
from mutiai.models import RuntimeBinding, RuntimeSecurityMode
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
        else:
            binding.provider = data.provider
            binding.model = data.model
            binding.reasoning_effort = data.reasoning_effort
            binding.security_mode = data.security_mode
            binding.is_active = True
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
        return binding

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
