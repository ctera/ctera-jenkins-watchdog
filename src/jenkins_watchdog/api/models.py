"""Pydantic models for API requests and responses."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    """Optional filters for a scan."""

    categories: list[str] | None = Field(None, description="Only run these check categories")
    investigate_all: bool = Field(False, description="Investigate all findings, not just new/critical")


class Investigation(BaseModel):
    """Structured output from Claude's tool-use investigation."""

    finding_fingerprint: str
    root_cause: str
    evidence: list[str]
    impact: str
    suggested_fix: str
    fix_location: str | None = Field(None, description="File path or resource where fix should be applied")
    confidence: Literal["high", "medium", "low"]
    tools_used: list[str] = Field(default_factory=list)
    raw_reasoning: str | None = Field(None, description="Claude's full reasoning trace")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0


class JiraIssueRef(BaseModel):
    """Reference to a linked Jira issue."""

    key: str
    url: str


class FindingResponse(BaseModel):
    """A finding with its optional investigation."""

    severity: Literal["critical", "warning", "low"]
    category: str
    resource: str
    symptom: str
    context: dict = Field(default_factory=dict)
    fingerprint: str
    status: Literal["new", "ongoing", "resolved"] = "new"
    first_seen: str | None = None
    last_seen: str | None = None
    investigation: Investigation | None = None
    jira_issue: JiraIssueRef | None = None


class ScanResult(BaseModel):
    """Response from POST /api/scan."""

    scan_id: str
    started_at: datetime
    completed_at: datetime
    total_findings: int
    new_findings: int
    critical_findings: int
    investigations_performed: int
    findings: list[FindingResponse]


class FindingsResponse(BaseModel):
    """Response from GET /api/findings."""

    last_scan: datetime | None
    total_findings: int
    findings: list[FindingResponse]
