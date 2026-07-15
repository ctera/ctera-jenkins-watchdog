"""SQLAlchemy unit of work composition."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.infrastructure.jenkins_repository import SqlAlchemyJenkinsRepository
from jenkins_watchdog.infrastructure.repositories import (
    SqlAlchemyActionRepository,
    SqlAlchemyCheckExecutionRepository,
    SqlAlchemyDeliveryAttemptRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyFindingRepository,
    SqlAlchemyIncidentRepository,
    SqlAlchemyInvestigationRepository,
    SqlAlchemyInvestigationRequestRepository,
    SqlAlchemyScanRepository,
)


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        self._session = self._session_factory()
        self.scans = SqlAlchemyScanRepository(self._session)
        self.checks = SqlAlchemyCheckExecutionRepository(self._session)
        self.findings = SqlAlchemyFindingRepository(self._session)
        self.incidents = SqlAlchemyIncidentRepository(self._session)
        self.investigations = SqlAlchemyInvestigationRepository(self._session)
        self.investigation_requests = SqlAlchemyInvestigationRequestRepository(self._session)
        self.actions = SqlAlchemyActionRepository(self._session)
        self.delivery_attempts = SqlAlchemyDeliveryAttemptRepository(self._session)
        self.events = SqlAlchemyEventRepository(self._session)
        self.jenkins = SqlAlchemyJenkinsRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is not None:
            await self._session.rollback()
        await self._session.close()
        self._session = None

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        await self._session.commit()

    async def rollback(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        await self._session.rollback()


class SqlAlchemyUnitOfWorkFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._session_factory)
