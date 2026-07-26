"""Versioned, product-owned content blocks for platform-assistant messages."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTENT_SCHEMA_VERSION = "1.0"
ResourceType = Literal[
    "organization",
    "organization_spec_version",
    "task",
    "plan",
    "artifact",
    "feasibility_check",
    "runtime_binding",
]


class _ContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=40_000)


class TextContentBlock(_ContentBlock):
    type: Literal["text"] = "text"


class MarkdownContentBlock(_ContentBlock):
    type: Literal["markdown"] = "markdown"
    truncated: bool = False


class CodeContentBlock(_ContentBlock):
    type: Literal["code"] = "code"
    language: str = Field(pattern=r"^[a-z0-9][a-z0-9.+_-]{0,31}$")
    file_name: str | None = Field(default=None, max_length=255)
    truncated: bool = False


class ErrorContentBlock(_ContentBlock):
    type: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=100)
    details: dict | list | str | int | float | bool | None = None


class AttachmentContentBlock(_ContentBlock):
    type: Literal["attachment"] = "attachment"
    attachment_id: str = Field(min_length=1, max_length=100)
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResourceRefContentBlock(_ContentBlock):
    type: Literal["resource_ref"] = "resource_ref"
    resource_type: ResourceType
    resource_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=120)


class OrganizationDiagramSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["organization_spec_version"]
    organization_id: str = Field(min_length=1, max_length=100)
    spec_version_id: str = Field(min_length=1, max_length=100)


class TaskPlanDiagramSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["task_plan"]
    task_id: str = Field(min_length=1, max_length=100)
    plan_id: str = Field(min_length=1, max_length=100)


DiagramSource = Annotated[
    OrganizationDiagramSource | TaskPlanDiagramSource,
    Field(discriminator="kind"),
]


class DiagramContentBlock(_ContentBlock):
    type: Literal["diagram"] = "diagram"
    template: Literal["organization_chart", "execution_plan"]
    source: DiagramSource

    @model_validator(mode="after")
    def source_matches_template(self) -> DiagramContentBlock:
        if self.template == "organization_chart" and not isinstance(
            self.source, OrganizationDiagramSource
        ):
            raise ValueError("organization_chart requires an organization source")
        if self.template == "execution_plan" and not isinstance(
            self.source, TaskPlanDiagramSource
        ):
            raise ValueError("execution_plan requires a Task plan source")
        return self


ContentBlock = Annotated[
    TextContentBlock
    | MarkdownContentBlock
    | CodeContentBlock
    | ErrorContentBlock
    | AttachmentContentBlock
    | ResourceRefContentBlock
    | DiagramContentBlock,
    Field(discriminator="type"),
]


class ResourceRefPresentationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["resource_ref"]
    resource_type: ResourceType
    resource_id: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=120)


class DiagramPresentationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["diagram"]
    template: Literal["organization_chart", "execution_plan"]
    source: DiagramSource
    text: str | None = Field(default=None, max_length=200)


ContentPresentationRequest = Annotated[
    ResourceRefPresentationRequest | DiagramPresentationRequest,
    Field(discriminator="kind"),
]


__all__ = [
    "CONTENT_SCHEMA_VERSION",
    "AttachmentContentBlock",
    "CodeContentBlock",
    "ContentBlock",
    "ContentPresentationRequest",
    "DiagramContentBlock",
    "DiagramPresentationRequest",
    "DiagramSource",
    "ErrorContentBlock",
    "MarkdownContentBlock",
    "OrganizationDiagramSource",
    "ResourceRefContentBlock",
    "ResourceRefPresentationRequest",
    "ResourceType",
    "TaskPlanDiagramSource",
    "TextContentBlock",
]
