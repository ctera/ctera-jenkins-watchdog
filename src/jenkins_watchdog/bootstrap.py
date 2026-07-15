"""The sole composition root for v2 services, clients, and workers."""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from jenkins_watchdog.config import Settings

if TYPE_CHECKING:
    import httpx
    from aiosmtplib import SMTP
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
    from valkey.asyncio import Valkey

    from jenkins_watchdog.application.automation import AutomationService
    from jenkins_watchdog.application.delivery import DeliveryService
    from jenkins_watchdog.application.events import EventService
    from jenkins_watchdog.application.incidents import IncidentService
    from jenkins_watchdog.application.investigations import InvestigationQueueService, InvestigationWorker
    from jenkins_watchdog.application.jenkins_monitor import JenkinsMonitorService, JenkinsMonitorWorker
    from jenkins_watchdog.application.pipeline import ScanPipeline
    from jenkins_watchdog.application.reasoning import ReasoningService
    from jenkins_watchdog.application.scan_service import ScanService
    from jenkins_watchdog.application.selection import AnalysisSelectionService
    from jenkins_watchdog.application.worker import ScanWorker
    from jenkins_watchdog.clients.jenkins import JenkinsClient
    from jenkins_watchdog.clients.k8s import KubernetesClient
    from jenkins_watchdog.clients.k8s_metrics import KubernetesMetricsClient
    from jenkins_watchdog.clients.prometheus import PrometheusClient
    from jenkins_watchdog.infrastructure.delivery import DeliveryRouter
    from jenkins_watchdog.infrastructure.events import ValkeyEventNotifier
    from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWorkFactory

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    valkey: Valkey
    http: httpx.AsyncClient
    smtp: SMTP
    jenkins: JenkinsClient
    kubernetes: KubernetesClient
    kubernetes_metrics: KubernetesMetricsClient
    prometheus: PrometheusClient
    uow_factory: SqlAlchemyUnitOfWorkFactory
    notifier: ValkeyEventNotifier
    events: EventService
    scan_service: ScanService
    incident_service: IncidentService
    reasoning_service: ReasoningService
    automation_service: AutomationService
    delivery_router: DeliveryRouter
    pipeline: ScanPipeline
    jenkins_monitor: JenkinsMonitorService
    investigation_queue: InvestigationQueueService
    selection_service: AnalysisSelectionService

    def make_worker(self, owner: str | None = None) -> ScanWorker:
        from jenkins_watchdog.application.worker import ScanWorker

        return ScanWorker(
            owner=owner or socket.gethostname(),
            uow_factory=self.uow_factory,
            pipeline=self.pipeline,
            events=self.events,
            now=_utcnow,
            lease_seconds=self.settings.worker_lease_seconds,
            heartbeat_seconds=self.settings.worker_heartbeat_seconds,
            poll_interval_seconds=self.settings.worker_poll_interval_s,
        )

    def make_delivery_worker(self, owner: str | None = None) -> DeliveryService:
        from jenkins_watchdog.application.delivery import DeliveryService

        return DeliveryService(
            owner=owner or socket.gethostname(),
            uow_factory=self.uow_factory,
            delivery=self.delivery_router,
            now=_utcnow,
            lease_seconds=self.settings.worker_lease_seconds,
            heartbeat_seconds=self.settings.worker_heartbeat_seconds,
            poll_interval_seconds=self.settings.worker_poll_interval_s,
        )

    def make_jenkins_worker(self, owner: str | None = None) -> JenkinsMonitorWorker:
        from jenkins_watchdog.application.jenkins_monitor import JenkinsMonitorWorker

        return JenkinsMonitorWorker(
            owner=owner or socket.gethostname(),
            monitor=self.jenkins_monitor,
            interval_seconds=self.settings.jenkins_sync_interval_s,
        )

    def make_investigation_worker(self, owner: str | None = None) -> InvestigationWorker:
        from jenkins_watchdog.application.investigations import InvestigationWorker

        return InvestigationWorker(
            owner=owner or socket.gethostname(),
            uow_factory=self.uow_factory,
            reasoning=self.reasoning_service,
            queue=self.investigation_queue,
            automation=self.automation_service,
            events=self.events,
            now=_utcnow,
            lease_seconds=self.settings.investigation_worker_lease_seconds,
            heartbeat_seconds=self.settings.worker_heartbeat_seconds,
            poll_interval_seconds=self.settings.worker_poll_interval_s,
            max_attempts=self.settings.investigation_max_attempts,
        )

    async def ready(self) -> None:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await self.valkey.ping()

    async def close(self) -> None:
        if self.smtp.is_connected:
            await self.smtp.quit()
        await self.jenkins.close()
        self.kubernetes.close()
        await self.http.aclose()
        await self.valkey.aclose()
        await self.engine.dispose()


@dataclass(slots=True)
class RuntimeHealth:
    engine: AsyncEngine
    valkey: Valkey

    async def ready(self) -> None:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await self.valkey.ping()

    async def close(self) -> None:
        await self.valkey.aclose()
        await self.engine.dispose()


def build_container(settings: Settings) -> Container:
    import httpx
    from aiosmtplib import SMTP

    from jenkins_watchdog.application.automation import AutomationService, IntegrationPolicy
    from jenkins_watchdog.application.events import EventService
    from jenkins_watchdog.application.incidents import IncidentService
    from jenkins_watchdog.application.investigations import InvestigationQueueService
    from jenkins_watchdog.application.jenkins_monitor import JenkinsMonitorService
    from jenkins_watchdog.application.pipeline import ScanPipeline
    from jenkins_watchdog.application.reasoning import ReasoningService
    from jenkins_watchdog.application.scan_service import ScanService
    from jenkins_watchdog.application.selection import AnalysisSelectionService
    from jenkins_watchdog.clients.jenkins import JenkinsClient
    from jenkins_watchdog.clients.k8s import KubernetesClient
    from jenkins_watchdog.clients.k8s_metrics import KubernetesMetricsClient
    from jenkins_watchdog.clients.prometheus import PrometheusClient
    from jenkins_watchdog.infrastructure.checks import LegacyCheckRunner, default_checks
    from jenkins_watchdog.infrastructure.database import create_engine, create_session_factory
    from jenkins_watchdog.infrastructure.delivery import (
        DeliveryRouter,
        EmailDelivery,
        GitHubDelivery,
        GitLabDelivery,
        JiraDelivery,
    )
    from jenkins_watchdog.infrastructure.events import ValkeyEventNotifier
    from jenkins_watchdog.infrastructure.jenkins_source import JenkinsSourceAdapter
    from jenkins_watchdog.infrastructure.reasoning import LiteLLMReasoningAdapter
    from jenkins_watchdog.infrastructure.routing import load_routing_config
    from jenkins_watchdog.infrastructure.source_attribution import JenkinsSourceAttributor, ScmSourceVerifier
    from jenkins_watchdog.infrastructure.source_profiles import load_source_profiles
    from jenkins_watchdog.infrastructure.templates import FilePayloadRenderer
    from jenkins_watchdog.infrastructure.tools import ReadOnlyToolRegistry
    from jenkins_watchdog.infrastructure.uow import SqlAlchemyUnitOfWorkFactory
    from jenkins_watchdog.scan_options import ScanOptions

    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    session_factory = create_session_factory(engine)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    valkey_client = _build_valkey(settings)
    http_client = httpx.AsyncClient(timeout=settings.request_timeout_s)
    smtp_client = SMTP(
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        start_tls=settings.smtp_start_tls,
        timeout=settings.request_timeout_s,
    )
    jenkins_client = JenkinsClient(
        base_url=settings.jenkins_url,
        username=settings.jenkins_user,
        token=settings.jenkins_token,
        failed_build_window_hours=settings.jenkins_failed_build_window_hours,
        timeout_seconds=settings.request_timeout_s,
    )
    kubernetes_client = KubernetesClient(
        request_timeout_seconds=settings.request_timeout_s,
        kubeconfig_path=settings.kubeconfig_path or None,
    )
    kubernetes_metrics = KubernetesMetricsClient(kubernetes_client)
    prometheus_client = PrometheusClient(
        http_client,
        endpoint=settings.prometheus_endpoint,
        enabled=settings.prometheus_enabled,
    )
    tool_registry = ReadOnlyToolRegistry(
        jenkins=jenkins_client,
        kubernetes=kubernetes_client,
        metrics=kubernetes_metrics,
        prometheus=prometheus_client,
        http=http_client,
        jenkins_namespace=settings.jenkins_namespace,
        github_api_url=settings.github_api_url,
        github_token=settings.github_token,
        gitlab_api_url=settings.gitlab_api_url,
        gitlab_token=settings.gitlab_token,
    )
    notifier = ValkeyEventNotifier(valkey_client)
    events = EventService(uow_factory, notifier)
    scan_service = ScanService(uow_factory, events=events)
    incident_service = IncidentService(uow_factory)
    investigation_queue = InvestigationQueueService(
        uow_factory=uow_factory,
        now=_utcnow,
        token_budget=settings.llm_scan_token_budget,
        deep_token_budget=settings.llm_deep_scan_token_budget,
        daily_token_budget=settings.llm_daily_token_budget,
        manual_token_reserve=settings.llm_manual_token_reserve,
    )
    reasoning_adapter = LiteLLMReasoningAdapter(
        model=settings.llm_model,
        fallback_models=tuple(item.strip() for item in settings.llm_fallback_models.split(",") if item.strip()),
        api_key=settings.anthropic_api_key,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        max_retries=settings.llm_max_retries,
        max_tool_rounds=settings.max_tool_rounds,
        max_deep_tool_rounds=settings.max_deep_tool_rounds,
        token_budget=settings.llm_scan_token_budget,
        deep_token_budget=settings.llm_deep_scan_token_budget,
        tools=tool_registry,
    )
    reasoning_service = ReasoningService(uow_factory=uow_factory, reasoning=reasoning_adapter, now=_utcnow)
    selection_service = AnalysisSelectionService(
        uow_factory=uow_factory,
        reasoning=reasoning_adapter,
        queue=investigation_queue,
        now=_utcnow,
        triage_batch_size=settings.llm_triage_batch_size,
        triage_token_reservation=settings.llm_max_tokens,
        automatic_enabled=settings.automatic_investigations_enabled,
    )
    source_profiles = load_source_profiles(settings.jenkins_source_profiles_path)
    source_verifier = ScmSourceVerifier(
        http_client,
        now=_utcnow,
        github_api_url=settings.github_api_url,
        github_token=settings.github_token,
        gitlab_api_url=settings.gitlab_api_url,
        gitlab_token=settings.gitlab_token,
    )
    jenkins_monitor = JenkinsMonitorService(
        source=JenkinsSourceAdapter(
            jenkins_client,
            attributor=JenkinsSourceAttributor(source_profiles, source_verifier),
        ),
        uow_factory=uow_factory,
        now=_utcnow,
        window_hours=settings.jenkins_sync_window_hours,
        fetch_concurrency=settings.jenkins_sync_concurrency,
        enrichment_limit=settings.jenkins_sync_enrichment_limit,
        log_enrichment_limit=settings.jenkins_sync_log_limit,
        source_attribution_limit=settings.jenkins_source_attribution_limit,
        lease_seconds=max(settings.worker_lease_seconds * 10, 900),
        heartbeat_seconds=settings.worker_heartbeat_seconds,
        incident_service=incident_service,
        selection_service=selection_service,
        automatic_investigations=settings.automatic_investigations_enabled,
        minimum_investigation_priority=settings.automatic_investigation_min_priority,
        analysis_candidate_limit=settings.automatic_investigation_batch_size,
        automatic_selection_limit=settings.max_investigations_per_scan,
    )
    routing = load_routing_config(settings.routing_config_path)
    fallback_recipients = _email_recipients(settings.email_fallback_recipients)
    if fallback_recipients:
        routing = replace(routing, global_fallback_recipients=fallback_recipients)
    renderer = FilePayloadRenderer(settings.automation_templates_path)
    jira_project = next((item.strip() for item in settings.jira_projects.split(",") if item.strip()), "CI")
    automation_service = AutomationService(
        uow_factory=uow_factory,
        routing=routing,
        renderer=renderer,
        policy=IntegrationPolicy(
            email_enabled=settings.email_enabled,
            jira_enabled=settings.jira_enabled,
            github_enabled=settings.github_enabled,
            gitlab_enabled=settings.gitlab_enabled,
            jira_project=jira_project,
        ),
        now=_utcnow,
    )
    delivery_router = DeliveryRouter(
        jira=(
            JiraDelivery(
                http_client,
                base_url=settings.jira_base_url,
                user=_required(settings.jira_user_email, "Jira user", settings.jira_enabled),
                token=_required(settings.jira_api_token, "Jira token", settings.jira_enabled),
            )
            if settings.jira_enabled
            else None
        ),
        email=(
            EmailDelivery(
                smtp_client,
                sender=settings.email_from,
                username=settings.smtp_username,
                password=settings.smtp_password,
            )
            if settings.email_enabled
            else None
        ),
        github=(
            GitHubDelivery(
                http_client,
                api_url=settings.github_api_url,
                token=_required(settings.github_token, "GitHub token", settings.github_enabled),
            )
            if settings.github_enabled
            else None
        ),
        gitlab=(
            GitLabDelivery(
                http_client,
                api_url=settings.gitlab_api_url,
                token=_required(settings.gitlab_token, "GitLab token", settings.gitlab_enabled),
            )
            if settings.gitlab_enabled
            else None
        ),
    )
    check_runner = LegacyCheckRunner(
        default_checks(
            jenkins=jenkins_client,
            kubernetes=kubernetes_client,
            metrics=kubernetes_metrics,
            jenkins_namespace=settings.jenkins_namespace,
            request_timeout_seconds=settings.request_timeout_s,
            k8s_events_window_minutes=settings.k8s_events_window_minutes,
        ),
        timeout_seconds=max(settings.request_timeout_s, 20.0),
        regular_options=ScanOptions(
            max_investigations_per_scan=settings.max_investigations_per_scan,
            max_tool_rounds=settings.max_tool_rounds,
            jenkins_failed_build_window_hours=settings.jenkins_failed_build_window_hours,
        ),
    )
    pipeline = ScanPipeline(
        uow_factory=uow_factory,
        check_runner=check_runner,
        incident_service=incident_service,
        selection_service=selection_service,
        automation_service=automation_service,
        events=events,
        now=_utcnow,
        max_investigations=settings.max_investigations_per_scan,
        max_deep_investigations=settings.max_deep_investigations_per_scan,
    )
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        valkey=valkey_client,
        http=http_client,
        smtp=smtp_client,
        jenkins=jenkins_client,
        kubernetes=kubernetes_client,
        kubernetes_metrics=kubernetes_metrics,
        prometheus=prometheus_client,
        uow_factory=uow_factory,
        notifier=notifier,
        events=events,
        scan_service=scan_service,
        incident_service=incident_service,
        reasoning_service=reasoning_service,
        automation_service=automation_service,
        delivery_router=delivery_router,
        pipeline=pipeline,
        jenkins_monitor=jenkins_monitor,
        investigation_queue=investigation_queue,
        selection_service=selection_service,
    )


def build_health_check(settings: Settings) -> RuntimeHealth:
    from jenkins_watchdog.infrastructure.database import create_engine

    return RuntimeHealth(
        engine=create_engine(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        ),
        valkey=_build_valkey(settings),
    )


def load_settings() -> Settings:
    return Settings()


def _build_valkey(settings: Settings) -> Valkey:
    from valkey.asyncio import Valkey

    use_ssl = settings.valkey_ssl and Path(settings.valkey_ca_cert).exists()
    if settings.valkey_ssl and not use_ssl:
        logger.warning("Valkey TLS certs not found, falling back to non-TLS")
    kwargs: dict[str, object] = {}
    if use_ssl:
        kwargs.update(
            ssl=True,
            ssl_ca_certs=settings.valkey_ca_cert,
            ssl_certfile=settings.valkey_client_cert,
            ssl_keyfile=settings.valkey_client_key,
        )
    return Valkey(
        host=settings.valkey_host,
        port=settings.valkey_port,
        decode_responses=True,
        socket_timeout=10,
        retry_on_timeout=True,
        **kwargs,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _required(value: str, name: str, enabled: bool) -> str:
    if enabled and not value:
        raise ValueError(f"{name} is required when the integration is enabled")
    return value


def _email_recipients(value: str) -> tuple[str, ...]:
    recipients = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    invalid = next(
        (item for item in recipients if "@" not in item or item.startswith("@") or item.endswith("@")),
        None,
    )
    if invalid:
        raise ValueError(f"invalid email fallback recipient {invalid!r}")
    return recipients
