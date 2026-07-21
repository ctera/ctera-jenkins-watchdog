"""Stable identity helpers for v2 findings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def stable_finding_identity(rule_id: str, resource_id: str, identity_dimensions: Mapping[str, Any]) -> str:
    """Return the full SHA-256 for a finding's stable identity contract."""
    payload = [rule_id, resource_id, _canonicalize(identity_dimensions)]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, tuple | list):
        return [_canonicalize(v) for v in value]
    if isinstance(value, set | frozenset):
        return sorted(_canonicalize(v) for v in value)
    return value
