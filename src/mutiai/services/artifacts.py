"""Validate, publish, and materialize product-owned Artifact files."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.domain import (
    ArtifactContractSpec,
    ArtifactDeclaration,
    AssignmentDelivery,
)
from mutiai.models import (
    Artifact,
    ArtifactInputBinding,
    Assignment,
    PlanStep,
    PlanStepDependency,
    Task,
    TaskExecutionPlan,
    Workspace,
)
from mutiai.models.base import utc_now
from mutiai.models.task_plan import ArtifactInputBindingStatus, ArtifactStatus
from mutiai.models.workspace import WorkspaceStatus
from mutiai.runtime import WorkspaceBoundaryError, WorkspaceManager
from mutiai.services.events import append_task_event

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(slots=True)
class ArtifactError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


class ArtifactManager:
    """Move immutable files across isolated role Workspaces through the product."""

    def __init__(self, workspace_manager: WorkspaceManager) -> None:
        self.workspace_manager = workspace_manager

    def resolve_stored_file(self, artifact: Artifact) -> Path:
        """Return a verified immutable Artifact path inside the managed root."""

        return self._validate_stored_artifact(artifact)

    def publish_task_input(
        self,
        session: Session,
        *,
        task: Task,
        plan: TaskExecutionPlan,
        contract: ArtifactContractSpec,
        source_path: str | Path,
        source_delivery_id: str,
    ) -> Artifact:
        """Publish a user-provided input against the frozen plan contract."""

        self._validate_delivery_id(source_delivery_id)
        if plan.task_id != task.task_id:
            raise ArtifactError(
                "ARTIFACT_PLAN_TASK_MISMATCH",
                "The Task input plan does not belong to the Task.",
            )
        if contract.contract_key not in plan.initial_input_contracts:
            raise ArtifactError(
                "ARTIFACT_INITIAL_CONTRACT_MISMATCH",
                "The Artifact contract is not declared as a plan input.",
            )

        try:
            source = Path(source_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ArtifactError(
                "ARTIFACT_SOURCE_MISSING",
                "The Task input Artifact source does not exist.",
            ) from exc
        if not source.is_file():
            raise ArtifactError(
                "ARTIFACT_SOURCE_NOT_FILE",
                "The Task input Artifact source must be a file.",
            )
        if source.name != contract.file_name:
            raise ArtifactError(
                "ARTIFACT_FILE_NAME_MISMATCH",
                "The Task input file name does not match its Artifact contract.",
            )

        try:
            artifact = self._publish_file(
                session,
                task=task,
                contract=contract,
                source=source,
                source_relative_path=source.name,
                source_delivery_id=source_delivery_id,
                origin="task_input",
                producer_assignment=None,
                producer_plan_step=None,
                source_workspace=None,
            )
            session.commit()
            session.refresh(artifact)
            return artifact
        except Exception:
            session.rollback()
            raise

    def publish_assignment_delivery(
        self,
        session: Session,
        *,
        task: Task,
        assignment: Assignment,
        delivery: AssignmentDelivery,
        source_delivery_id: str,
    ) -> tuple[Artifact, ...]:
        """Validate one completed Runtime delivery and release its files."""

        self._validate_delivery_id(source_delivery_id)
        if delivery.status == "blocked":
            raise ArtifactError(
                "ARTIFACT_DELIVERY_BLOCKED",
                "A blocked Assignment has no publishable Artifact delivery.",
            )
        if assignment.task_id != task.task_id or assignment.plan_step_id is None:
            raise ArtifactError(
                "ARTIFACT_ASSIGNMENT_PLAN_MISMATCH",
                "The Assignment is not bound to a plan step for this Task.",
            )

        step = session.get(PlanStep, assignment.plan_step_id)
        if step is None:
            raise ArtifactError(
                "ARTIFACT_PLAN_STEP_MISSING",
                "The Assignment plan step is unavailable.",
            )
        expected_contracts = {
            contract.contract_key: contract
            for contract in (
                ArtifactContractSpec.model_validate(payload)
                for payload in step.output_contracts
            )
        }
        declarations = {
            declaration.contract_key: declaration
            for declaration in delivery.artifacts
        }
        if declarations.keys() != expected_contracts.keys():
            raise ArtifactError(
                "ARTIFACT_OUTPUT_SET_MISMATCH",
                "The delivery does not declare exactly the plan step output contracts.",
            )

        execution = assignment.runtime_execution
        if execution is None or execution.workspace_id is None:
            raise ArtifactError(
                "ARTIFACT_PRODUCER_WORKSPACE_MISSING",
                "The Assignment Runtime has no recorded producer Workspace.",
            )
        workspace = session.get(Workspace, execution.workspace_id)
        if workspace is None or workspace.status != WorkspaceStatus.READY:
            raise ArtifactError(
                "ARTIFACT_PRODUCER_WORKSPACE_UNAVAILABLE",
                "The producer Workspace is not ready.",
            )
        if (
            workspace.organization_id != task.organization_id
            or workspace.agent_role_key != assignment.agent_role_key
        ):
            raise ArtifactError(
                "ARTIFACT_PRODUCER_WORKSPACE_MISMATCH",
                "The producer Workspace ownership does not match the Assignment.",
            )

        published: list[Artifact] = []
        try:
            for contract_key, contract in expected_contracts.items():
                declaration = declarations[contract_key]
                self._validate_declaration_contract(declaration, contract)
                source = self._resolve_workspace_file(
                    workspace,
                    declaration.relative_path,
                )
                published.append(
                    self._publish_file(
                        session,
                        task=task,
                        contract=contract,
                        source=source,
                        source_relative_path=declaration.relative_path,
                        source_delivery_id=source_delivery_id,
                        origin="assignment",
                        producer_assignment=assignment,
                        producer_plan_step=step,
                        source_workspace=workspace,
                    )
                )
            session.commit()
            for artifact in published:
                session.refresh(artifact)
            return tuple(published)
        except Exception:
            session.rollback()
            raise

    def materialize_step_inputs(
        self,
        session: Session,
        *,
        task: Task,
        plan_step: PlanStep,
        consumer_workspace: Workspace,
    ) -> tuple[ArtifactInputBinding, ...]:
        """Freeze and copy the exact released input versions for one plan step."""

        if consumer_workspace.status != WorkspaceStatus.READY:
            raise ArtifactError(
                "ARTIFACT_CONSUMER_WORKSPACE_UNAVAILABLE",
                "The consumer Workspace is not ready.",
            )
        if (
            consumer_workspace.organization_id != task.organization_id
            or consumer_workspace.agent_role_key != plan_step.role_key
        ):
            raise ArtifactError(
                "ARTIFACT_CONSUMER_WORKSPACE_MISMATCH",
                "The consumer Workspace ownership does not match the plan step.",
            )

        plan = session.get(TaskExecutionPlan, plan_step.plan_id)
        if plan is None or plan.task_id != task.task_id:
            raise ArtifactError(
                "ARTIFACT_PLAN_TASK_MISMATCH",
                "The consumer plan step does not belong to the Task.",
            )
        ancestor_ids = self._ancestor_step_ids(session, plan_step)
        bindings: list[ArtifactInputBinding] = []
        try:
            for contract_key in plan_step.input_contracts:
                binding = self._existing_contract_binding(
                    session,
                    plan_step_id=plan_step.plan_step_id,
                    contract_key=contract_key,
                )
                if binding is None:
                    artifact = self._select_released_input(
                        session,
                        task=task,
                        plan=plan,
                        contract_key=contract_key,
                        ancestor_step_ids=ancestor_ids,
                    )
                    binding = ArtifactInputBinding(
                        input_binding_id=self._binding_id(
                            plan_step.plan_step_id,
                            artifact.artifact_id,
                        ),
                        task_id=task.task_id,
                        plan_step_id=plan_step.plan_step_id,
                        artifact_id=artifact.artifact_id,
                        consumer_workspace_id=consumer_workspace.workspace_id,
                        materialized_relative_path=self._materialized_relative_path(
                            artifact
                        ),
                        artifact_sha256=artifact.sha256,
                        status=ArtifactInputBindingStatus.MATERIALIZED,
                    )
                    session.add(binding)
                    session.flush()
                    self._materialize_binding(
                        artifact=artifact,
                        binding=binding,
                        consumer_workspace=consumer_workspace,
                    )
                    append_task_event(
                        session,
                        task=task,
                        event_type="artifact.input_materialized",
                        aggregate_type="artifact_input_binding",
                        aggregate_id=binding.input_binding_id,
                        source="product",
                        payload={
                            "input_binding_id": binding.input_binding_id,
                            "artifact_id": artifact.artifact_id,
                            "contract_key": artifact.contract_key,
                            "plan_step_id": plan_step.plan_step_id,
                            "consumer_workspace_id": consumer_workspace.workspace_id,
                            "artifact_sha256": artifact.sha256,
                        },
                    )
                else:
                    artifact = session.get(Artifact, binding.artifact_id)
                    if artifact is None or artifact.sha256 != binding.artifact_sha256:
                        raise ArtifactError(
                            "ARTIFACT_BINDING_CORRUPT",
                            "The persisted Artifact input binding is inconsistent.",
                        )
                    if binding.consumer_workspace_id != consumer_workspace.workspace_id:
                        raise ArtifactError(
                            "ARTIFACT_BINDING_WORKSPACE_MISMATCH",
                            "The frozen input binding belongs to another Workspace.",
                        )
                    self._materialize_binding(
                        artifact=artifact,
                        binding=binding,
                        consumer_workspace=consumer_workspace,
                    )
                bindings.append(binding)
            session.commit()
            return tuple(bindings)
        except Exception:
            session.rollback()
            raise

    def _publish_file(
        self,
        session: Session,
        *,
        task: Task,
        contract: ArtifactContractSpec,
        source: Path,
        source_relative_path: str,
        source_delivery_id: str,
        origin: str,
        producer_assignment: Assignment | None,
        producer_plan_step: PlanStep | None,
        source_workspace: Workspace | None,
    ) -> Artifact:
        existing = session.scalar(
            select(Artifact).where(
                Artifact.task_id == task.task_id,
                Artifact.contract_key == contract.contract_key,
                Artifact.source_delivery_id == source_delivery_id,
            )
        )
        if existing is not None:
            expected_assignment_id = (
                producer_assignment.assignment_id
                if producer_assignment is not None
                else None
            )
            if (
                existing.origin != origin
                or existing.producer_assignment_id != expected_assignment_id
                or existing.media_type != contract.media_type
                or existing.file_name != contract.file_name
                or existing.source_relative_path != source_relative_path
            ):
                raise ArtifactError(
                    "ARTIFACT_DELIVERY_ID_CONFLICT",
                    "The Artifact delivery ID was reused for different content metadata.",
                )
            self._validate_stored_artifact(existing)
            return existing

        latest = session.scalar(
            select(Artifact)
            .where(
                Artifact.task_id == task.task_id,
                Artifact.contract_key == contract.contract_key,
            )
            .order_by(Artifact.artifact_version.desc())
            .limit(1)
        )
        next_version = 1 if latest is None else latest.artifact_version + 1
        artifact_id = self._artifact_id(
            task.task_id,
            contract.contract_key,
            source_delivery_id,
        )
        storage_relative_path = self._storage_relative_path(
            task=task,
            artifact_id=artifact_id,
            file_name=contract.file_name,
        )
        target_parent = self.workspace_manager.provision(
            storage_relative_path.parent,
        )
        target = self.workspace_manager.canonicalize(
            target_parent / contract.file_name,
            must_exist=False,
        )
        snapshot = self._atomic_copy(source, target)
        validation_summary = self._validate_media(snapshot, contract.media_type)
        sha256, byte_size = self._file_facts(snapshot)

        if latest is not None and latest.status == ArtifactStatus.RELEASED:
            latest.status = ArtifactStatus.SUPERSEDED

        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task.task_id,
            origin=origin,
            source_delivery_id=source_delivery_id,
            producer_assignment_id=(
                producer_assignment.assignment_id
                if producer_assignment is not None
                else None
            ),
            producer_plan_step_id=(
                producer_plan_step.plan_step_id
                if producer_plan_step is not None
                else None
            ),
            source_workspace_id=(
                source_workspace.workspace_id if source_workspace is not None else None
            ),
            contract_key=contract.contract_key,
            schema_version=contract.schema_version,
            artifact_version=next_version,
            media_type=contract.media_type,
            file_name=contract.file_name,
            source_relative_path=source_relative_path,
            storage_relative_path=storage_relative_path.as_posix(),
            sha256=sha256,
            byte_size=byte_size,
            status=ArtifactStatus.RELEASED,
            validation_summary=validation_summary,
            supersedes_artifact_id=(latest.artifact_id if latest is not None else None),
            released_at=utc_now(),
        )
        session.add(artifact)
        session.flush()
        append_task_event(
            session,
            task=task,
            event_type="artifact.released",
            aggregate_type="artifact",
            aggregate_id=artifact.artifact_id,
            assignment_id=(
                producer_assignment.assignment_id
                if producer_assignment is not None
                else None
            ),
            source="product",
            payload={
                "artifact_id": artifact.artifact_id,
                "origin": artifact.origin,
                "contract_key": artifact.contract_key,
                "artifact_version": artifact.artifact_version,
                "producer_plan_step_id": artifact.producer_plan_step_id,
                "sha256": artifact.sha256,
                "byte_size": artifact.byte_size,
                "status": artifact.status,
            },
        )
        return artifact

    def _resolve_workspace_file(
        self,
        workspace: Workspace,
        relative_path: str,
    ) -> Path:
        try:
            declaration = ArtifactDeclaration(
                contract_key="path.validation",
                relative_path=relative_path,
                media_type="application/octet-stream",
            )
        except ValueError as exc:
            raise ArtifactError(
                "ARTIFACT_SOURCE_PATH_INVALID",
                "The declared Artifact path is not a safe relative path.",
            ) from exc
        workspace_root = self.workspace_manager.canonicalize(
            workspace.canonical_path,
            must_exist=True,
        )
        try:
            source = (workspace_root / declaration.relative_path).resolve(strict=True)
        except OSError as exc:
            raise ArtifactError(
                "ARTIFACT_SOURCE_MISSING",
                "The declared Artifact source does not exist.",
            ) from exc
        if source == workspace_root or not source.is_relative_to(workspace_root):
            raise ArtifactError(
                "ARTIFACT_SOURCE_OUTSIDE_WORKSPACE",
                "The declared Artifact path escapes the producer Workspace.",
            )
        if not source.is_file():
            raise ArtifactError(
                "ARTIFACT_SOURCE_NOT_FILE",
                "The declared Artifact source is not a file.",
            )
        return source

    @staticmethod
    def _validate_declaration_contract(
        declaration: ArtifactDeclaration,
        contract: ArtifactContractSpec,
    ) -> None:
        if declaration.media_type != contract.media_type:
            raise ArtifactError(
                "ARTIFACT_MEDIA_TYPE_MISMATCH",
                "The declared media type does not match the output contract.",
            )
        path_name = re_split_path_name(declaration.relative_path)
        if path_name != contract.file_name:
            raise ArtifactError(
                "ARTIFACT_FILE_NAME_MISMATCH",
                "The declared file name does not match the output contract.",
            )

    def _select_released_input(
        self,
        session: Session,
        *,
        task: Task,
        plan: TaskExecutionPlan,
        contract_key: str,
        ancestor_step_ids: set[str],
    ) -> Artifact:
        candidates = session.scalars(
            select(Artifact)
            .where(
                Artifact.task_id == task.task_id,
                Artifact.contract_key == contract_key,
                Artifact.status == ArtifactStatus.RELEASED,
            )
            .order_by(Artifact.artifact_version.desc())
        ).all()
        for artifact in candidates:
            if (
                artifact.origin == "task_input"
                and contract_key in plan.initial_input_contracts
            ) or (
                artifact.origin == "assignment"
                and artifact.producer_plan_step_id in ancestor_step_ids
            ):
                return artifact
        raise ArtifactError(
            "ARTIFACT_REQUIRED_INPUT_MISSING",
            f"No released eligible Artifact satisfies contract '{contract_key}'.",
        )

    @staticmethod
    def _ancestor_step_ids(session: Session, plan_step: PlanStep) -> set[str]:
        direct = session.scalars(
            select(PlanStepDependency.depends_on_step_id).where(
                PlanStepDependency.plan_step_id == plan_step.plan_step_id
            )
        ).all()
        ancestors = set(direct)
        frontier = list(direct)
        while frontier:
            current = frontier.pop()
            parents = session.scalars(
                select(PlanStepDependency.depends_on_step_id).where(
                    PlanStepDependency.plan_step_id == current
                )
            ).all()
            for parent in parents:
                if parent not in ancestors:
                    ancestors.add(parent)
                    frontier.append(parent)
        return ancestors

    @staticmethod
    def _existing_contract_binding(
        session: Session,
        *,
        plan_step_id: str,
        contract_key: str,
    ) -> ArtifactInputBinding | None:
        return session.scalar(
            select(ArtifactInputBinding)
            .join(Artifact, Artifact.artifact_id == ArtifactInputBinding.artifact_id)
            .where(
                ArtifactInputBinding.plan_step_id == plan_step_id,
                Artifact.contract_key == contract_key,
                ArtifactInputBinding.status
                == ArtifactInputBindingStatus.MATERIALIZED,
            )
        )

    def _materialize_binding(
        self,
        *,
        artifact: Artifact,
        binding: ArtifactInputBinding,
        consumer_workspace: Workspace,
    ) -> None:
        self._validate_stored_artifact(artifact)
        source = self.workspace_manager.canonicalize(
            artifact.storage_relative_path,
            must_exist=True,
        )
        workspace_root = self.workspace_manager.canonicalize(
            consumer_workspace.canonical_path,
            must_exist=True,
        )
        destination = (
            workspace_root / binding.materialized_relative_path
        ).resolve(strict=False)
        if not destination.is_relative_to(workspace_root):
            raise ArtifactError(
                "ARTIFACT_DESTINATION_OUTSIDE_WORKSPACE",
                "The materialized input path escapes the consumer Workspace.",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and self._file_facts(destination)[0] == artifact.sha256:
            return
        copied = self._atomic_copy(source, destination)
        if self._file_facts(copied)[0] != artifact.sha256:
            raise ArtifactError(
                "ARTIFACT_MATERIALIZATION_HASH_MISMATCH",
                "The materialized Artifact does not match the released hash.",
            )

    def _validate_stored_artifact(self, artifact: Artifact) -> Path:
        if artifact.status not in {
            ArtifactStatus.RELEASED,
            ArtifactStatus.SUPERSEDED,
        }:
            raise ArtifactError(
                "ARTIFACT_NOT_RELEASED",
                "The Artifact is not an immutable released version.",
            )
        try:
            stored = self.workspace_manager.canonicalize(
                artifact.storage_relative_path,
                must_exist=True,
            )
            sha256, byte_size = self._file_facts(stored)
        except (OSError, WorkspaceBoundaryError) as exc:
            raise ArtifactError(
                "ARTIFACT_STORED_FILE_UNAVAILABLE",
                "The stored Artifact file is unavailable inside the managed root.",
            ) from exc
        if sha256 != artifact.sha256 or byte_size != artifact.byte_size:
            raise ArtifactError(
                "ARTIFACT_STORED_FILE_CORRUPT",
                "The stored Artifact bytes do not match persisted metadata.",
            )
        return stored

    @staticmethod
    def _validate_media(path: Path, media_type: str) -> str:
        try:
            if media_type == "application/json":
                with path.open("r", encoding="utf-8") as stream:
                    json.load(stream)
                return "Validated JSON syntax and UTF-8 encoding."
            if media_type == XLSX_MEDIA_TYPE:
                if not zipfile.is_zipfile(path):
                    raise ValueError("not an OOXML ZIP archive")
                with zipfile.ZipFile(path) as workbook:
                    names = set(workbook.namelist())
                required = {"[Content_Types].xml", "xl/workbook.xml"}
                if not required <= names:
                    raise ValueError("missing required XLSX workbook entries")
                return "Validated the XLSX container and workbook entries."
            if media_type == "image/png":
                if path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                    raise ValueError("invalid PNG signature")
                return "Validated the PNG signature."
            if media_type == "image/jpeg":
                data = path.read_bytes()
                if not (data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")):
                    raise ValueError("invalid JPEG boundaries")
                return "Validated the JPEG boundaries."
            if media_type.startswith("text/"):
                path.read_text(encoding="utf-8")
                return "Validated UTF-8 text encoding."
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            raise ArtifactError(
                "ARTIFACT_MEDIA_VALIDATION_FAILED",
                f"Artifact media validation failed: {exc}",
            ) from exc
        return "Validated the declared media type contract."

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> Path:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def _file_facts(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _validate_delivery_id(source_delivery_id: str) -> None:
        if not source_delivery_id or len(source_delivery_id) > 100:
            raise ArtifactError(
                "ARTIFACT_DELIVERY_ID_INVALID",
                "Artifact source_delivery_id must contain 1 to 100 characters.",
            )

    @staticmethod
    def _artifact_id(
        task_id: str,
        contract_key: str,
        source_delivery_id: str,
    ) -> str:
        identity = f"mutiai:artifact:{task_id}:{contract_key}:{source_delivery_id}"
        return str(uuid5(NAMESPACE_URL, identity))

    @staticmethod
    def _binding_id(plan_step_id: str, artifact_id: str) -> str:
        return str(
            uuid5(
                NAMESPACE_URL,
                f"mutiai:artifact-binding:{plan_step_id}:{artifact_id}",
            )
        )

    @staticmethod
    def _storage_relative_path(
        *,
        task: Task,
        artifact_id: str,
        file_name: str,
    ) -> Path:
        return (
            Path("users")
            / task.owner_user_id
            / "organizations"
            / task.organization_id
            / "tasks"
            / task.task_id
            / "artifacts"
            / artifact_id
            / file_name
        )

    @staticmethod
    def _materialized_relative_path(artifact: Artifact) -> str:
        return (Path("inputs") / artifact.artifact_id / artifact.file_name).as_posix()


def re_split_path_name(relative_path: str) -> str:
    """Return a portable base name for slash or backslash input."""

    return relative_path.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
