"""Pydantic models for the skills API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class SkillListing(BaseModel):
    """Compact listing returned by GET /api/skills."""
    name: str = Field(..., description="Unique skill name")
    description: str = Field("", description="Short description (max 1024 chars), with optional [action_type] prefix")
    tags: List[str] = Field(default_factory=list, description="Categorization tags")
    version: str = Field("1.0.0", description="Semver version string")
    # P3 cognition multi-axis labels (optional; default empty/any for backward compat)
    action_type: str = Field("", description="execute|analyze|retrieve|generate|transform|record")
    discipline: str = Field("", description="coding|ops|writing|design|data|general")
    carrier: str = Field("any", description="local|api|any")


class SkillDetail(BaseModel):
    """Full skill detail returned by GET /api/skills/{name}."""
    name: str
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    version: str = "1.0.0"
    platforms: List[str] = Field(default_factory=list)
    author: str = ""
    license: str = ""
    content: str = Field(..., description="Full Markdown body of the skill")
    path: str = ""
    # Curator activity metadata (sourced from SkillUsage, not the SKILL.md).
    state: str = Field("active", description="curator lifecycle: active/stale/archived")
    use_count: int = Field(0, description="times this skill was loaded/invoked")
    last_activity_at: Optional[str] = Field(None, description="ISO timestamp of last use")
    pinned: bool = Field(False, description="if true, curator never auto-transitions")
    # P3 cognition multi-axis labels.
    action_type: str = Field("", description="execute|analyze|retrieve|generate|transform|record")
    discipline: str = Field("", description="coding|ops|writing|design|data|general")
    carrier: str = Field("any", description="local|api|any")
    triggers: List[str] = Field(default_factory=list, description="routing signal keywords")
    requires: List[str] = Field(default_factory=list, description="env deps")


class SkillListResponse(BaseModel):
    """Response envelope for GET /api/skills."""
    skills: List[SkillListing] = Field(default_factory=list)
    count: int = 0


class SkillDetailResponse(BaseModel):
    """Response envelope for GET /api/skills/{name}."""
    skill: Optional[SkillDetail] = None
    found: bool = False
