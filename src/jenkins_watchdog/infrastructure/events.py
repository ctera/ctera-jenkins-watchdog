"""Bounded Valkey Streams used only as low-latency event notifications."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from valkey.asyncio import Valkey

from jenkins_watchdog.application.types import ScanEvent

STREAM_MAX_LENGTH = 1_000
STREAM_TTL_SECONDS = 7 * 24 * 60 * 60


class ValkeyEventNotifier:
    def __init__(self, client: Valkey) -> None:
        self._client = client

    async def publish(self, event: ScanEvent) -> None:
        key = _stream_key(event.scan_id)
        await self._client.xadd(
            key,
            {
                "sequence": str(event.sequence),
                "type": event.type,
                "event": json.dumps(
                    {
                        "sequence": event.sequence,
                        "type": event.type,
                        "occurred_at": event.occurred_at.isoformat(),
                        "payload_version": event.payload_version,
                        "payload": event.payload,
                    },
                    separators=(",", ":"),
                ),
            },
            maxlen=STREAM_MAX_LENGTH,
            approximate=True,
        )
        await self._client.expire(key, STREAM_TTL_SECONDS)

    async def wait(self, scan_id: str, stream_id: str, *, timeout_seconds: float = 5.0) -> str:
        result: Any = await self._client.xread(
            {_stream_key(scan_id): stream_id},
            count=1,
            block=max(1, int(timeout_seconds * 1000)),
        )
        if not result:
            return stream_id
        return str(result[0][1][-1][0])


class PollingEventNotifier:
    async def publish(self, event: ScanEvent) -> None:
        del event

    async def wait(self, scan_id: str, stream_id: str, *, timeout_seconds: float = 5.0) -> str:
        del scan_id
        await asyncio.sleep(timeout_seconds)
        return stream_id


def _stream_key(scan_id: str) -> str:
    return f"watchdog:v2:scan:{scan_id}:events"
