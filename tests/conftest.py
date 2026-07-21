"""Shared test fixtures."""

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jenkins_watchdog.infrastructure.database import create_engine, create_session_factory
from jenkins_watchdog.infrastructure.models import Base


@pytest_asyncio.fixture
async def postgres_session_factory() -> async_sessionmaker[AsyncSession]:
    database_url = os.environ.get("WATCHDOG_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("WATCHDOG_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


@pytest.fixture
def sample_findings():
    """Sample findings for testing."""
    from jenkins_watchdog.checks.base import Finding

    return [
        Finding(
            severity="critical",
            category="jenkins_agent",
            resource="jenkins/jenkins-agent-abc123",
            symptom="CrashLoopBackOff (container: jnlp)",
            context={"restart_count": 17},
        ),
        Finding(
            severity="warning",
            category="jenkins_agent",
            resource="jenkins-agent/worker-1",
            symptom="Agent offline: connection timed out",
            context={"offline_reason": "connection timed out"},
        ),
    ]
