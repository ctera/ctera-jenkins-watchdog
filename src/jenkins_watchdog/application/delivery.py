"""Leased delivery execution with immutable attempt history."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from jenkins_watchdog.application.ports import ActionDeliveryPort, UnitOfWorkFactory
from jenkins_watchdog.domain.model import (
    Action,
    DeliveryAttempt,
    DeliveryAttemptStatus,
)

DELIVERY_RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=4),
)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    external_reference: str | None
    metadata: dict[str, Any]


class DeliveryError(Exception):
    def __init__(self, summary: str, *, retryable: bool, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(summary)
        self.summary = summary[:500]
        self.retryable = retryable
        self.metadata = metadata or {}


class DeliveryService:
    def __init__(
        self,
        *,
        owner: str,
        uow_factory: UnitOfWorkFactory,
        delivery: ActionDeliveryPort,
        now: Callable[[], datetime],
        lease_seconds: int = 60,
        heartbeat_seconds: int = 15,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.owner = owner
        self._uow_factory = uow_factory
        self._delivery = delivery
        self._now = now
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def run_once(self) -> bool:
        now = self._now()
        async with self._uow_factory() as uow:
            action = await uow.actions.claim(owner=self.owner, now=now, lease_seconds=self._lease_seconds)
            await uow.commit()
        if action is None:
            return False

        attempt = DeliveryAttempt(
            id=str(uuid4()),
            action_id=action.id,
            retry_cycle=action.retry_cycle,
            attempt_number=action.attempt_count,
            status=DeliveryAttemptStatus.RUNNING,
            started_at=now,
        )
        async with self._uow_factory() as uow:
            await uow.delivery_attempts.save(attempt)
            await uow.commit()

        heartbeat = asyncio.create_task(self._heartbeat(action.id))
        delivery = asyncio.create_task(self._delivery.deliver(action))
        try:
            done, _ = await asyncio.wait({delivery, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                error = heartbeat.exception()
                if error is not None:
                    delivery.cancel()
                    await asyncio.gather(delivery, return_exceptions=True)
                    raise error
            raw = await delivery
            result = DeliveryResult(
                external_reference=_optional_string(raw.get("external_reference")),
                metadata=dict(raw.get("metadata", {})),
            )
        except DeliveryError as exc:
            await self._record_failure(action, attempt, exc)
        except Exception as exc:
            await self._record_failure(
                action,
                attempt,
                DeliveryError(f"{type(exc).__name__}: delivery failed", retryable=True),
            )
        else:
            completed_at = self._now()
            attempt = replace(
                attempt,
                status=DeliveryAttemptStatus.SUCCEEDED,
                response_metadata=result.metadata,
                completed_at=completed_at,
            )
            action = action.delivered(external_reference=result.external_reference, now=completed_at)
            async with self._uow_factory() as uow:
                await uow.delivery_attempts.save(attempt)
                await uow.actions.save(action)
                await uow.commit()
        finally:
            delivery.cancel()
            heartbeat.cancel()
            await asyncio.gather(delivery, heartbeat, return_exceptions=True)
        return True

    async def manual_retry(self, action_id: str) -> Action:
        now = self._now()
        async with self._uow_factory() as uow:
            action = await uow.actions.get(action_id)
            if action is None:
                raise LookupError(f"action {action_id} does not exist")
            action = action.manual_retry(now=now)
            await uow.actions.save(action)
            await uow.commit()
            return action

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            if await self.run_once():
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass

    async def _record_failure(self, action: Action, attempt: DeliveryAttempt, error: DeliveryError) -> None:
        failed_at = self._now()
        retry_at = None
        if error.retryable and action.attempt_count <= len(DELIVERY_RETRY_DELAYS):
            retry_at = failed_at + DELIVERY_RETRY_DELAYS[action.attempt_count - 1]
        attempt = replace(
            attempt,
            status=(
                DeliveryAttemptStatus.RETRYABLE_FAILED
                if retry_at is not None
                else DeliveryAttemptStatus.PERMANENT_FAILED
            ),
            response_metadata=error.metadata,
            error_summary=error.summary,
            completed_at=failed_at,
        )
        action = action.delivery_failed(summary=error.summary, now=failed_at, retry_at=retry_at)
        async with self._uow_factory() as uow:
            await uow.delivery_attempts.save(attempt)
            await uow.actions.save(action)
            await uow.commit()

    async def _heartbeat(self, action_id: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            async with self._uow_factory() as uow:
                owned = await uow.actions.heartbeat(
                    action_id,
                    owner=self.owner,
                    now=self._now(),
                    lease_seconds=self._lease_seconds,
                )
                await uow.commit()
            if not owned:
                raise RuntimeError(f"lost action lease for {action_id}")


def _optional_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
