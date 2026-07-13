"""Opaque cursor encoding for stable `(created_at, id)` pagination."""

from __future__ import annotations

import base64
import json
from datetime import datetime


class InvalidCursorError(ValueError):
    pass


def encode_cursor(created_at: datetime, item_id: str) -> str:
    payload = json.dumps([created_at.isoformat(), item_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, str) for item in value):
            raise ValueError
        return datetime.fromisoformat(value[0]), value[1]
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("invalid cursor") from exc
