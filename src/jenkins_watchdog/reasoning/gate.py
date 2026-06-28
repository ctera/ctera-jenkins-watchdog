"""Gate logic — decide whether a finding warrants Claude investigation."""

from jenkins_watchdog.checks.base import Finding
from jenkins_watchdog.state import FindingsDiff

# Categories that always deserve investigation when new/critical
_HIGH_VALUE_CATEGORIES = frozenset({
    "jenkins_failed_build",
    "jenkins_pipeline_pattern",
    "jenkins_queue",
})


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

    # Pipeline patterns and build failures are high-value — always investigate when new
    if finding.category in _HIGH_VALUE_CATEGORIES:
        if finding in diff.new:
            return True
        if finding in diff.ongoing and finding.severity == "critical":
            return True
        # Shared failure signatures across jobs — re-investigate if still ongoing
        if finding.context.get("pattern") == "shared_failure_signature":
            return finding in diff.new or finding.severity == "critical"

    if finding in diff.new and finding.severity in ("critical", "warning"):
        return True
    if finding in diff.ongoing and finding.severity == "critical":
        return True
    return False
