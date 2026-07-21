from datetime import datetime, timedelta, timezone

import pytest

from jenkins_watchdog.domain.model import Action, ActionStatus, ActionType

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def action() -> Action:
    return Action(
        id="action-1",
        incident_id="incident-1",
        occurrence_id="occurrence-1",
        action_type=ActionType.EMAIL,
        destination="ops@example.com",
        status=ActionStatus.PENDING,
        rendered_payload={"subject": "subject", "body": "body"},
        template_version="v1",
        idempotency_key="key",
        external_identity="identity",
        created_at=NOW,
        updated_at=NOW,
    )


def test_action_heartbeat_extends_only_owned_running_lease():
    claimed = action().claim(owner="worker-a", now=NOW, lease_seconds=60)

    heartbeat = claimed.heartbeat(owner="worker-a", now=NOW + timedelta(seconds=15), lease_seconds=60)

    assert heartbeat.lease_expires_at == NOW + timedelta(seconds=75)
    with pytest.raises(ValueError):
        claimed.heartbeat(owner="worker-b", now=NOW, lease_seconds=60)
