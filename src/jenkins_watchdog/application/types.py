"""Shared application-facing types that remain independent of adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jenkins_watchdog.domain.model import Scan, ScanMode


@dataclass(frozen=True, slots=True)
class EnqueueScan:
    mode: ScanMode
    categories: tuple[str, ...]
    triggering_user_email: str | None = None
    scheduled: bool = False


@dataclass(frozen=True, slots=True)
class CursorPage:
    items: tuple[Any, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ScanEvent:
    scan_id: str
    sequence: int
    type: str
    occurred_at: datetime
    payload_version: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ClaimedScan:
    scan: Scan
    lease_owner: str


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    references: tuple[dict[str, str], ...]
    as_of: datetime
    coverage_status: str
