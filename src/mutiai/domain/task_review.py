"""Structured organization-lead review contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LeadReviewResult(BaseModel):
    """The bounded decision and delivery summary returned by the lead Runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    decision: Literal["accepted", "needs_revision"]
    final_summary: str = Field(min_length=1, max_length=20_000)
    issues: tuple[str, ...] = Field(max_length=50)
