"""Standard-library conversion of immutable domain values to JSON primitives."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any


def to_primitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): to_primitive(nested) for key, nested in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [to_primitive(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value
