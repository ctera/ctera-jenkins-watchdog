import json
from datetime import datetime, timezone

import pytest

from jenkins_watchdog.application.types import ScanEvent
from jenkins_watchdog.infrastructure.events import (
    STREAM_MAX_LENGTH,
    STREAM_TTL_SECONDS,
    PollingEventNotifier,
    ValkeyEventNotifier,
)


class Valkey:
    def __init__(self) -> None:
        self.added = None
        self.expired = None
        self.read_result = []
        self.read_args = None

    async def xadd(self, key, values, **kwargs):
        self.added = (key, values, kwargs)

    async def expire(self, key, ttl):
        self.expired = (key, ttl)

    async def xread(self, streams, **kwargs):
        self.read_args = (streams, kwargs)
        return self.read_result


@pytest.mark.asyncio
async def test_valkey_stream_is_bounded_expiring_and_contains_event_envelope() -> None:
    valkey = Valkey()
    notifier = ValkeyEventNotifier(valkey)
    event = ScanEvent(
        scan_id="scan-1",
        sequence=7,
        type="check_completed",
        occurred_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        payload_version=1,
        payload={"check": "nodes"},
    )

    await notifier.publish(event)

    key, values, options = valkey.added
    assert key == "watchdog:v2:scan:scan-1:events"
    assert options == {"maxlen": STREAM_MAX_LENGTH, "approximate": True}
    assert json.loads(values["event"])["sequence"] == 7
    assert valkey.expired == (key, STREAM_TTL_SECONDS)


@pytest.mark.asyncio
async def test_valkey_wait_returns_previous_or_latest_stream_id() -> None:
    valkey = Valkey()
    notifier = ValkeyEventNotifier(valkey)

    assert await notifier.wait("scan-1", "1-0", timeout_seconds=0) == "1-0"
    valkey.read_result = [["key", [["9-0", {"sequence": "9"}]]]]
    assert await notifier.wait("scan-1", "1-0", timeout_seconds=0.25) == "9-0"
    assert valkey.read_args == (
        {"watchdog:v2:scan:scan-1:events": "1-0"},
        {"count": 1, "block": 250},
    )


@pytest.mark.asyncio
async def test_polling_notifier_is_a_noop_wakeup() -> None:
    notifier = PollingEventNotifier()
    event = ScanEvent("scan", 1, "event", datetime.now(timezone.utc), 1, {})
    await notifier.publish(event)
    assert await notifier.wait("scan", "$", timeout_seconds=0) == "$"
