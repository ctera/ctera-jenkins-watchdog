from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jenkins_watchdog.application.investigations import (
    InvestigationBudgetExceeded,
    InvestigationCostBudgetExceeded,
    InvestigationQueueService,
)
from jenkins_watchdog.application.selection import AnalysisSelectionService
from jenkins_watchdog.application.types import EnqueueScan, TriageBatchResult, TriageRoute
from jenkins_watchdog.domain.model import (
    AnalysisDecisionOutcome,
    CheckResult,
    CheckStatus,
    FindingObservation,
    Incident,
    InvestigationBudgetKind,
    LLMCall,
    ScanMode,
    Severity,
)
from jenkins_watchdog.infrastructure.models import Base
from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWork

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def sqlite_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def factory_port(factory: async_sessionmaker[AsyncSession]):
    return lambda: SqlAlchemyUnitOfWork(factory)


async def seed_incidents(
    factory: async_sessionmaker[AsyncSession],
    severities: tuple[Severity, ...],
) -> tuple[str, tuple[Incident, ...]]:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        scan = await uow.scans.add(EnqueueScan(mode=ScanMode.REGULAR, categories=("k8s_node",)))
        observations = tuple(
            FindingObservation(
                scan_id=scan.id,
                check_name="k8s_nodes",
                rule_id="k8s.node.condition.v1",
                resource_id=f"node/worker-{index}",
                severity=severity,
                category="k8s_node",
                summary=f"worker {index} condition",
                observed_at=NOW,
                evidence={"condition": f"condition-{index}"},
            )
            for index, severity in enumerate(severities, start=1)
        )
        await uow.checks.save(
            scan.id,
            CheckResult(
                scan_id=scan.id,
                check_name="k8s_nodes",
                status=CheckStatus.SUCCEEDED,
                categories=frozenset({"k8s_node"}),
                started_at=NOW,
                completed_at=NOW,
            ),
        )
        await uow.findings.add_observations(scan.id, observations)
        incidents = []
        for observation in observations:
            incident = Incident.open_new(
                id=str(uuid.uuid4()),
                correlation_rule_id="stable_finding",
                correlation_key=observation.stable_identity,
                observation=observation,
                opened_at=NOW,
            )
            await uow.incidents.save(incident)
            await uow.incidents.link_observation(incident, observation)
            incidents.append(incident)
        await uow.commit()
    return scan.id, tuple(incidents)


class BatchReasoning:
    def __init__(self, *, investigate: bool = False) -> None:
        self.investigate = investigate
        self.batches: list[tuple[str, ...]] = []

    async def triage_batch(self, candidates):
        incident_ids = tuple(candidate.incident.id for candidate in candidates)
        self.batches.append(incident_ids)
        call = LLMCall(
            id=str(uuid.uuid4()),
            purpose="triage",
            model="test-model",
            prompt_tokens=80,
            completion_tokens=20,
            cache_read_input_tokens=10,
            cache_creation_input_tokens=0,
            total_tokens=100,
            estimated_cost_usd=Decimal("0.0025"),
            cost_source="test",
            created_at=NOW,
        )
        return TriageBatchResult(
            routes=tuple(
                TriageRoute(
                    incident_id=incident_id,
                    action="investigate" if self.investigate else "defer",
                    reason="bounded batch decision",
                )
                for incident_id in incident_ids
            ),
            model_calls=(call,),
        )


@pytest.mark.asyncio
async def test_selector_persists_every_decision_and_enforces_regular_cycle_cap(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, incidents = await seed_incidents(sqlite_factory, (Severity.CRITICAL, Severity.CRITICAL))
    factory = factory_port(sqlite_factory)
    selector = AnalysisSelectionService(
        uow_factory=factory,
        reasoning=BatchReasoning(),
        queue=InvestigationQueueService(uow_factory=factory, now=lambda: NOW),
        now=lambda: NOW,
    )

    result = await selector.select(
        tuple(incident.id for incident in incidents),
        source="scan",
        mode=ScanMode.REGULAR,
        limit=1,
        priority_by_incident={incidents[0].id: 100, incidents[1].id: 90},
    )

    assert result.selected_count == 1
    outcomes = {decision.outcome for decision in result.decisions}
    assert outcomes == {AnalysisDecisionOutcome.SELECTED, AnalysisDecisionOutcome.DEFERRED}
    assert any(decision.reason_code == "cycle_limit_reached" for decision in result.decisions)
    async with SqlAlchemyUnitOfWork(sqlite_factory) as uow:
        persisted = [await uow.analysis_decisions.latest_for_incident(incident.id) for incident in incidents]
    assert all(persisted)


@pytest.mark.asyncio
async def test_uncertain_warnings_share_one_bounded_triage_cost(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    scan_id, incidents = await seed_incidents(sqlite_factory, (Severity.WARNING, Severity.WARNING))
    factory = factory_port(sqlite_factory)
    reasoning = BatchReasoning(investigate=False)
    selector = AnalysisSelectionService(
        uow_factory=factory,
        reasoning=reasoning,
        queue=InvestigationQueueService(uow_factory=factory, now=lambda: NOW),
        now=lambda: NOW,
        triage_batch_size=50,
    )

    result = await selector.select(
        tuple(incident.id for incident in incidents),
        source="scan",
        mode=ScanMode.REGULAR,
        limit=12,
        scan_id=scan_id,
    )

    assert reasoning.batches == [tuple(incident.id for incident in incidents)]
    assert all(decision.outcome is AnalysisDecisionOutcome.DEFERRED for decision in result.decisions)
    assert {decision.llm_call_id for decision in result.decisions} != {None}
    async with SqlAlchemyUnitOfWork(sqlite_factory) as uow:
        usage = await uow.llm_calls.summary_for_scan(scan_id)
    assert usage["call_count"] == 1
    assert usage["total_tokens"] == 100
    assert usage["estimated_cost_usd"] == 0.0025
    assert usage["by_purpose"]["triage"]["call_count"] == 1


@pytest.mark.asyncio
async def test_daily_budget_protects_manual_reserve_and_records_reservations(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, incidents = await seed_incidents(
        sqlite_factory,
        (Severity.CRITICAL, Severity.CRITICAL, Severity.CRITICAL),
    )
    queue = InvestigationQueueService(
        uow_factory=factory_port(sqlite_factory),
        now=lambda: NOW,
        token_budget=12_000,
        deep_token_budget=20_000,
        daily_token_budget=30_000,
        manual_token_reserve=10_000,
    )

    automatic = await queue.enqueue_incident(incidents[0].id, source="scan")
    assert automatic is not None
    assert automatic.budget_kind is InvestigationBudgetKind.AUTOMATIC
    assert automatic.reserved_tokens == 12_000
    with pytest.raises(InvestigationBudgetExceeded) as error:
        await queue.enqueue_incident(incidents[1].id, source="jenkins_monitor")
    assert error.value.limit == 20_000
    assert error.value.spent == 0
    assert error.value.active_reserved == 12_000
    assert error.value.requested == 12_000
    assert error.value.reset_at == datetime(2026, 7, 16, tzinfo=timezone.utc)

    manual = await queue.enqueue_incident(
        incidents[2].id,
        source="manual_incident",
        requested_by="operator@example.com",
        force=True,
    )
    assert manual is not None
    assert manual.budget_kind is InvestigationBudgetKind.MANUAL
    assert manual.reserved_tokens == 12_000


@pytest.mark.asyncio
async def test_daily_cost_budget_protects_manual_reserve_with_conservative_reservations(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, incidents = await seed_incidents(
        sqlite_factory,
        (Severity.CRITICAL, Severity.CRITICAL, Severity.CRITICAL, Severity.CRITICAL),
    )
    queue = InvestigationQueueService(
        uow_factory=factory_port(sqlite_factory),
        now=lambda: NOW,
        token_budget=10_000,
        daily_token_budget=1_000_000,
        manual_token_reserve=0,
        daily_cost_budget_usd=Decimal("1.00"),
        manual_cost_reserve_usd=Decimal("0.25"),
        max_token_cost_usd_per_million=Decimal("25.00"),
    )

    for incident in incidents[:3]:
        request = await queue.enqueue_incident(incident.id, source="scan")
        assert request is not None

    with pytest.raises(InvestigationCostBudgetExceeded) as error:
        await queue.enqueue_incident(incidents[3].id, source="scan")
    assert error.value.limit_usd == Decimal("0.75")
    assert error.value.spent_usd == Decimal("0")
    assert error.value.active_reserved_usd == Decimal("0.75")
    assert error.value.requested_usd == Decimal("0.25")
    assert error.value.projected_usd == Decimal("1.00")
    assert error.value.metadata()["budget_metric"] == "cost_usd"

    manual = await queue.enqueue_incident(
        incidents[3].id,
        source="manual_incident",
        requested_by="operator@example.com",
        force=True,
    )
    assert manual is not None
    assert manual.budget_kind is InvestigationBudgetKind.MANUAL


@pytest.mark.asyncio
async def test_default_cost_guard_admits_ten_regular_requests_before_the_cycle_ceiling(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, incidents = await seed_incidents(sqlite_factory, (Severity.CRITICAL,) * 11)
    queue = InvestigationQueueService(
        uow_factory=factory_port(sqlite_factory),
        now=lambda: NOW,
    )

    admitted = [
        await queue.enqueue_incident(incident.id, source="scan")
        for incident in incidents[:10]
    ]

    assert all(request is not None for request in admitted)
    assert sum(request.reserved_tokens for request in admitted if request is not None) == 400_000
    with pytest.raises(InvestigationCostBudgetExceeded) as error:
        await queue.enqueue_incident(incidents[10].id, source="scan")
    assert error.value.limit_usd == Decimal("10.50")
    assert error.value.active_reserved_usd == Decimal("10.00")
    assert error.value.projected_usd == Decimal("11.00")


@pytest.mark.asyncio
async def test_daily_cost_budget_conservatively_prices_calls_without_a_cost_estimate(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, incidents = await seed_incidents(sqlite_factory, (Severity.CRITICAL,))
    async with SqlAlchemyUnitOfWork(sqlite_factory) as uow:
        await uow.llm_calls.save_many(
            (
                LLMCall(
                    id=str(uuid.uuid4()),
                    purpose="investigation",
                    model="unpriced-model",
                    prompt_tokens=8_000,
                    completion_tokens=2_000,
                    cache_read_input_tokens=1_000,
                    cache_creation_input_tokens=0,
                    total_tokens=10_000,
                    created_at=NOW,
                ),
            )
        )
        await uow.commit()
    queue = InvestigationQueueService(
        uow_factory=factory_port(sqlite_factory),
        now=lambda: NOW,
        token_budget=10_000,
        daily_token_budget=1_000_000,
        manual_token_reserve=0,
        daily_cost_budget_usd=Decimal("0.50"),
        manual_cost_reserve_usd=Decimal("0"),
        max_token_cost_usd_per_million=Decimal("25.00"),
    )

    with pytest.raises(InvestigationCostBudgetExceeded) as error:
        await queue.enqueue_incident(incidents[0].id, source="scan")

    assert error.value.spent_usd == Decimal("0.275")
    assert error.value.requested_usd == Decimal("0.25")
    assert error.value.projected_usd == Decimal("0.525")
