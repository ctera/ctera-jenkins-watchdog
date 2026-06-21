"""Gate logic — decide whether a finding warrants Claude investigation."""

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.state import FindingsDiff


def should_investigate(
    finding: Finding,
    diff: FindingsDiff,
    existing_investigations: dict | None = None,
) -> bool:
    """Decide whether to investigate. Skips already-investigated high-confidence ongoing findings."""
    existing_investigations = existing_investigations or {}

    existing = existing_investigations.get(finding.fingerprint)
    if existing and isinstance(existing, dict):
        if existing.get("confidence") == "high" and finding in diff.ongoing:
            return False

    if finding in diff.new and finding.severity in ("critical", "warning"):
        return True
    if finding in diff.ongoing and finding.severity == "critical":
        return True
    return False
