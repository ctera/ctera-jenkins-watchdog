from datetime import datetime, timedelta, timezone

import pytest

from jenkins_watchdog.domain.model import (
    InvestigationRequest,
    InvestigationRequestStatus,
    ScanMode,
)

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def request() -> InvestigationRequest:
    return InvestigationRequest(
        id="request",
        incident_id="incident",
        occurrence_id="occurrence",
        mode=ScanMode.REGULAR,
        source="automatic",
        priority=70,
        evidence_hash="evidence",
        status=InvestigationRequestStatus.QUEUED,
        created_at=NOW,
        updated_at=NOW,
        next_attempt_at=NOW,
    )


def test_request_claim_heartbeat_retry_and_success_lifecycle() -> None:
    claimed = request().claim(owner="worker-a", now=NOW, lease_seconds=60)
    assert claimed.status is InvestigationRequestStatus.RUNNING
    assert claimed.attempt_count == 1
    assert claimed.lease_expires_at == NOW + timedelta(seconds=60)

    heartbeat = claimed.heartbeat(owner="worker-a", now=NOW + timedelta(seconds=30), lease_seconds=60)
    assert heartbeat.lease_expires_at == NOW + timedelta(seconds=90)
    retry_at = NOW + timedelta(minutes=2)
    retried = heartbeat.fail("temporary", now=NOW + timedelta(seconds=40), retry_at=retry_at)
    assert retried.status is InvestigationRequestStatus.QUEUED
    assert retried.next_attempt_at == retry_at

    reclaimed = retried.claim(owner="worker-b", now=retry_at, lease_seconds=60)
    completed = reclaimed.succeed("investigation", now=retry_at + timedelta(seconds=5))
    assert completed.status is InvestigationRequestStatus.SUCCEEDED
    assert completed.investigation_id == "investigation"
    assert completed.lease_owner is None


def test_request_rejects_unowned_or_early_claims() -> None:
    with pytest.raises(ValueError, match="not claimable"):
        request().claim(owner="worker", now=NOW - timedelta(seconds=1), lease_seconds=60)
    claimed = request().claim(owner="worker", now=NOW, lease_seconds=60)
    with pytest.raises(ValueError, match="not owned"):
        claimed.heartbeat(owner="other", now=NOW, lease_seconds=60)
