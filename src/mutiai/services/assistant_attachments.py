"""Product-owned storage and access rules for platform-assistant attachments."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.models import (
    AssistantAttachment,
    AssistantAttachmentStatus,
)
from mutiai.models.base import new_id, utc_now

ASSISTANT_ATTACHMENT_CONTENT_MAX_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_EXACT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/tab-separated-values",
    }
)


class AssistantAttachmentError(ValueError):
    """A safe, product-level attachment error."""

    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class AssistantAttachmentManager:
    """Keep assistant uploads outside source and Runtime workspace roots."""

    def __init__(self, root: str | Path, *, max_bytes: int) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        file_name: str | None,
        media_type: str | None,
        source: BinaryIO,
    ) -> AssistantAttachment:
        normalized_media_type = self._normalize_media_type(media_type)
        normalized_file_name = self._normalize_file_name(file_name)
        attachment_id = new_id()
        relative_path = (
            Path(owner_user_id) / conversation_id / f"{attachment_id}.bin"
        ).as_posix()
        destination = self._resolve_relative(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        byte_size = 0
        try:
            with destination.open("wb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > self.max_bytes:
                        raise AssistantAttachmentError(
                            "ASSISTANT_ATTACHMENT_TOO_LARGE",
                            "The assistant attachment exceeds the configured size limit.",
                            status_code=413,
                        )
                    digest.update(chunk)
                    handle.write(chunk)
        except AssistantAttachmentError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_STORAGE_FAILED",
                "The assistant attachment could not be stored.",
                status_code=500,
            ) from exc

        attachment = AssistantAttachment(
            attachment_id=attachment_id,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            file_name=normalized_file_name,
            media_type=normalized_media_type,
            byte_size=byte_size,
            sha256=digest.hexdigest(),
            storage_relative_path=relative_path,
            status=AssistantAttachmentStatus.UPLOADED,
        )
        session.add(attachment)
        session.flush()
        return attachment

    def validate_refs(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        refs: list[dict],
    ) -> list[AssistantAttachment]:
        attachment_ids: list[str] = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise AssistantAttachmentError(
                    "ASSISTANT_ATTACHMENT_INVALID",
                    "The assistant attachment reference is invalid.",
                )
            attachment_id = ref.get("attachment_id")
            if not isinstance(attachment_id, str) or not attachment_id:
                raise AssistantAttachmentError(
                    "ASSISTANT_ATTACHMENT_INVALID",
                    "The assistant attachment reference is missing its ID.",
                )
            attachment_ids.append(attachment_id)
        if len(set(attachment_ids)) != len(attachment_ids):
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_INVALID",
                "An assistant attachment cannot be referenced more than once.",
            )
        if not attachment_ids:
            return []
        rows = session.scalars(
            select(AssistantAttachment).where(
                AssistantAttachment.attachment_id.in_(attachment_ids),
                AssistantAttachment.conversation_id == conversation_id,
                AssistantAttachment.owner_user_id == owner_user_id,
            )
        ).all()
        by_id = {row.attachment_id: row for row in rows}
        if len(by_id) != len(attachment_ids):
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_NOT_FOUND",
                "One or more assistant attachments were not found.",
                status_code=404,
            )
        ordered = [by_id[attachment_id] for attachment_id in attachment_ids]
        if any(row.status != AssistantAttachmentStatus.UPLOADED for row in ordered):
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_NOT_AVAILABLE",
                "One or more assistant attachments are no longer available.",
                status_code=409,
            )
        return ordered

    def attach_to_message(
        self,
        session: Session,
        *,
        message_id: str,
        attachments: Iterable[AssistantAttachment],
    ) -> None:
        now = utc_now()
        for attachment in attachments:
            attachment.status = AssistantAttachmentStatus.ATTACHED
            attachment.message_id = message_id
            attachment.attached_at = now

    def revoke(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        attachment_id: str,
    ) -> AssistantAttachment:
        attachment = session.scalar(
            select(AssistantAttachment).where(
                AssistantAttachment.attachment_id == attachment_id,
                AssistantAttachment.conversation_id == conversation_id,
                AssistantAttachment.owner_user_id == owner_user_id,
            )
        )
        if attachment is None:
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_NOT_FOUND",
                "Assistant attachment not found.",
                status_code=404,
            )
        if attachment.status != AssistantAttachmentStatus.UPLOADED:
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_NOT_REVOCABLE",
                "Only an unattached assistant upload can be revoked.",
                status_code=409,
            )
        path = self._resolve_relative(attachment.storage_relative_path)
        path.unlink(missing_ok=True)
        attachment.status = AssistantAttachmentStatus.REVOKED
        attachment.revoked_at = utc_now()
        return attachment

    def read_text(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        attachment_id: str,
    ) -> tuple[AssistantAttachment, str | dict | list]:
        attachment = session.scalar(
            select(AssistantAttachment).where(
                AssistantAttachment.attachment_id == attachment_id,
                AssistantAttachment.conversation_id == conversation_id,
                AssistantAttachment.owner_user_id == owner_user_id,
            )
        )
        if attachment is None:
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_NOT_FOUND",
                "Assistant attachment not found.",
                status_code=404,
            )
        if attachment.status != AssistantAttachmentStatus.ATTACHED:
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_NOT_AVAILABLE",
                "The assistant attachment is not attached to a message.",
                status_code=409,
            )
        media_type = attachment.media_type.partition(";")[0].strip().casefold()
        if media_type != "application/json" and not media_type.startswith("text/"):
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_CONTENT_UNSUPPORTED",
                "The platform assistant can read only attached UTF-8 JSON or text attachments.",
                status_code=415,
            )
        if attachment.byte_size > ASSISTANT_ATTACHMENT_CONTENT_MAX_BYTES:
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_CONTENT_TOO_LARGE",
                "The attachment is too large for the platform assistant content reader.",
                status_code=413,
            )
        path = self._resolve_relative(attachment.storage_relative_path)
        try:
            with path.open("rb") as handle:
                raw_content = handle.read(ASSISTANT_ATTACHMENT_CONTENT_MAX_BYTES + 1)
            if len(raw_content) > ASSISTANT_ATTACHMENT_CONTENT_MAX_BYTES:
                raise AssistantAttachmentError(
                    "ASSISTANT_ATTACHMENT_CONTENT_TOO_LARGE",
                    "The attachment is too large for the platform assistant content reader.",
                    status_code=413,
                )
            content_text = raw_content.decode("utf-8")
            content: str | dict | list = (
                json.loads(content_text)
                if media_type == "application/json"
                else content_text
            )
        except AssistantAttachmentError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_CONTENT_INVALID",
                "The attachment content could not be read safely.",
                status_code=409,
            ) from exc
        return attachment, content

    def path_for(self, attachment: AssistantAttachment) -> Path:
        return self._resolve_relative(attachment.storage_relative_path)

    def metadata(self, attachment: AssistantAttachment) -> dict[str, Any]:
        return {
            "attachment_id": attachment.attachment_id,
            "conversation_id": attachment.conversation_id,
            "file_name": attachment.file_name,
            "media_type": attachment.media_type,
            "byte_size": attachment.byte_size,
            "sha256": attachment.sha256,
            "status": attachment.status.value
            if isinstance(attachment.status, AssistantAttachmentStatus)
            else str(attachment.status),
            "created_at": attachment.created_at.isoformat(),
            "attached_at": (
                attachment.attached_at.isoformat() if attachment.attached_at else None
            ),
            "revoked_at": (
                attachment.revoked_at.isoformat() if attachment.revoked_at else None
            ),
        }

    def _resolve_relative(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve(strict=False)
        if candidate == self.root or not candidate.is_relative_to(self.root):
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_STORAGE_FAILED",
                "The assistant attachment path is outside the managed root.",
                status_code=500,
            )
        return candidate

    @staticmethod
    def _normalize_file_name(file_name: str | None) -> str:
        value = Path(file_name or "attachment").name
        value = value.replace("\x00", "").strip()
        if not value or value in {".", ".."}:
            return "attachment"
        return value[:255]

    @staticmethod
    def _normalize_media_type(media_type: str | None) -> str:
        value = (media_type or "application/octet-stream").partition(";")[0]
        value = value.strip().casefold()
        if value not in _ALLOWED_EXACT_MEDIA_TYPES:
            raise AssistantAttachmentError(
                "ASSISTANT_ATTACHMENT_MEDIA_UNSUPPORTED",
                "This assistant attachment media type is not supported.",
                status_code=415,
            )
        return value
