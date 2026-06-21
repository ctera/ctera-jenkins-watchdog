"""Base check protocol and Finding dataclass."""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

_DYNAMIC_VALUES = re.compile(r"\b\d+(\.\d+)?(%|s|h|ms|gb|mb|kb)?\b", re.IGNORECASE)


@dataclass
class Finding:
    severity: Literal["critical", "warning", "low"]
    category: str
    resource: str
    symptom: str
    context: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        symptom_class = _DYNAMIC_VALUES.sub("N", self.symptom.split("(")[0].split(",")[0].strip())
        raw = f"{self.category}:{self.resource}:{symptom_class}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "resource": self.resource,
            "symptom": self.symptom,
            "context": self.context,
            "fingerprint": self.fingerprint,
        }


class BaseCheck(Protocol):
    """Protocol for all checks."""

    name: str

    async def run(self) -> list[Finding]: ...
