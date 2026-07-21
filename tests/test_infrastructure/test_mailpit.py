from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from aiosmtplib import SMTP

from jenkins_watchdog.domain.model import Action, ActionStatus, ActionType
from jenkins_watchdog.infrastructure.delivery import EmailDelivery


@pytest.mark.asyncio
async def test_email_delivery_is_visible_in_mailpit() -> None:
    host = os.environ.get("WATCHDOG_TEST_SMTP_HOST")
    port = os.environ.get("WATCHDOG_TEST_SMTP_PORT")
    mailpit_url = os.environ.get("WATCHDOG_TEST_MAILPIT_URL")
    if not host or not port or not mailpit_url:
        pytest.skip("Mailpit integration environment is not configured")
    now = datetime.now(timezone.utc)
    marker = uuid.uuid4().hex
    action = Action(
        id=str(uuid.uuid4()),
        incident_id=str(uuid.uuid4()),
        occurrence_id=str(uuid.uuid4()),
        action_type=ActionType.EMAIL,
        destination="operator@example.com",
        status=ActionStatus.PENDING,
        rendered_payload={"subject": f"Watchdog integration {marker}", "body": "Mailpit delivery body"},
        template_version="v1",
        idempotency_key=f"mailpit:{marker}",
        external_identity=f"email:{marker}",
        created_at=now,
        updated_at=now,
        next_attempt_at=now,
    )
    smtp = SMTP(hostname=host, port=int(port), start_tls=False, timeout=5)
    try:
        result = await EmailDelivery(smtp, sender="watchdog@example.com", username="", password="").deliver(action)
    finally:
        if smtp.is_connected:
            await smtp.quit()

    async with httpx.AsyncClient(base_url=mailpit_url) as client:
        inbox = (await client.get("/api/v1/messages")).json()

    message = next(item for item in inbox["messages"] if item["Subject"] == f"Watchdog integration {marker}")
    assert message["To"][0]["Address"] == "operator@example.com"
    assert result["metadata"]["accepted"] is True
    assert result["external_reference"].startswith("<")
