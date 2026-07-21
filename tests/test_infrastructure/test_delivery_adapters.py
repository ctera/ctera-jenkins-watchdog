from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from jenkins_watchdog.application.delivery import DeliveryError
from jenkins_watchdog.domain.model import Action, ActionStatus, ActionType
from jenkins_watchdog.infrastructure.delivery import (
    DeliveryRouter,
    EmailDelivery,
    GitHubDelivery,
    GitLabDelivery,
    JiraDelivery,
    _raise_http,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def action(action_type: ActionType, *, destination: str, payload: dict | None = None) -> Action:
    return Action(
        id="12345678-1234-5678-1234-567812345678",
        incident_id="22345678-1234-5678-1234-567812345678",
        occurrence_id="32345678-1234-5678-1234-567812345678",
        action_type=action_type,
        destination=destination,
        status=ActionStatus.PENDING,
        rendered_payload=payload or {"body": "investigation"},
        template_version="v1",
        idempotency_key="idempotency-key",
        external_identity="jira:incident:22345678-1234-5678-1234-567812345678",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (404, False), (429, True), (500, True)],
)
def test_http_failure_classification_and_sanitized_metadata(status: int, retryable: bool) -> None:
    request = httpx.Request("GET", "https://example.test/resource")
    response = httpx.Response(
        status,
        request=request,
        headers={"x-request-id": "request-123"},
        text="sensitive response body",
    )

    with pytest.raises(DeliveryError) as exc:
        _raise_http(response)

    assert exc.value.retryable is retryable
    assert exc.value.metadata == {"status_code": status, "request_id": "request-123"}
    assert "sensitive" not in exc.value.summary


@pytest.mark.asyncio
async def test_delivery_router_rejects_disabled_integration() -> None:
    router = DeliveryRouter(jira=None, email=None, github=None, gitlab=None)

    with pytest.raises(DeliveryError) as exc:
        await router.deliver(action(ActionType.EMAIL, destination="ops@example.com"))

    assert not exc.value.retryable


@pytest.mark.asyncio
async def test_github_reuses_marker_and_creates_new_comment() -> None:
    requests: list[httpx.Request] = []
    existing_action = action(ActionType.GITHUB_COMMENT, destination="github:ctera/app:42")
    marker = f"<!-- watchdog:{existing_action.idempotency_key} -->"

    async def reuse_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json=[{"id": 9, "html_url": "https://comment/9", "body": marker}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(reuse_handler)) as client:
        reused = await GitHubDelivery(client, api_url="https://api.github.test", token="secret").deliver(
            existing_action
        )

    assert reused == {"external_reference": "https://comment/9", "metadata": {"reused": True}}
    assert len(requests) == 1

    async def create_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, request=request, json=[])
        return httpx.Response(201, request=request, json={"id": 10, "html_url": "https://comment/10"})

    requests.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(create_handler)) as client:
        created = await GitHubDelivery(client, api_url="https://api.github.test", token="secret").deliver(
            existing_action
        )

    assert created["external_reference"] == "https://comment/10"
    assert created["metadata"] == {"status_code": 201}
    assert requests[1].method == "POST"
    assert marker in requests[1].content.decode()


@pytest.mark.asyncio
async def test_gitlab_encodes_repository_and_reuses_note() -> None:
    target = action(ActionType.GITLAB_COMMENT, destination="gitlab:ctera/platform/app:91")
    marker = f"<!-- watchdog:{target.idempotency_key} -->"
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, request=request, json=[{"id": 55, "body": marker}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GitLabDelivery(client, api_url="https://gitlab.test/api/v4", token="secret").deliver(target)

    assert result == {"external_reference": "55", "metadata": {"reused": True}}
    assert "ctera%2Fplatform%2Fapp" in seen[0]


@pytest.mark.asyncio
async def test_jira_reuses_create_and_posts_reopen_update() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, request=request, json={"issues": [{"key": "CI-42"}]})
        return httpx.Response(201, request=request, json={})

    create = action(
        ActionType.JIRA_CREATE,
        destination="CI",
        payload={"summary": "summary", "description": "description"},
    )
    update = action(
        ActionType.JIRA_UPDATE,
        destination="CI",
        payload={"summary": "summary", "description": "reopened"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = JiraDelivery(client, base_url="https://jira.test", user="user", token="secret")
        reused = await adapter.deliver(create)
        reopened = await adapter.deliver(update)

    assert reused == {"external_reference": "CI-42", "metadata": {"reused": True}}
    assert reopened["external_reference"] == "CI-42"
    assert [request.method for request in calls] == ["GET", "GET", "POST"]
    assert calls[-1].url.path.endswith("/issue/CI-42/comment")


@pytest.mark.asyncio
async def test_jira_create_and_missing_update_classification() -> None:
    async def create_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, request=request, json={"issues": []})
        return httpx.Response(201, request=request, json={"key": "CI-100"})

    create = action(
        ActionType.JIRA_CREATE,
        destination="CI",
        payload={"summary": "summary", "description": "description"},
    )
    update = action(
        ActionType.JIRA_UPDATE,
        destination="CI",
        payload={"summary": "summary", "description": "description"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(create_handler)) as client:
        adapter = JiraDelivery(client, base_url="https://jira.test", user="user", token="secret")
        created = await adapter.deliver(create)
        with pytest.raises(DeliveryError) as exc:
            await adapter.deliver(update)

    assert created["external_reference"] == "CI-100"
    assert not exc.value.retryable


class FakeSmtp:
    def __init__(self, code: int | None = None) -> None:
        self.is_connected = False
        self.code = code
        self.connected = 0
        self.logins = []
        self.message = None

    async def connect(self) -> None:
        self.connected += 1
        self.is_connected = True

    async def login(self, username: str, password: str) -> None:
        self.logins.append((username, password))

    async def send_message(self, message):
        self.message = message
        errors = {"ops@example.com": SimpleNamespace(code=self.code)} if self.code else {}
        return errors, "queued"


@pytest.mark.asyncio
async def test_email_delivery_connects_authenticates_and_classifies_smtp_codes() -> None:
    target = action(
        ActionType.EMAIL,
        destination="ops@example.com",
        payload={"subject": "Incident", "body": "Details"},
    )
    smtp = FakeSmtp()
    delivered = await EmailDelivery(smtp, sender="watchdog@example.com", username="user", password="secret").deliver(
        target
    )

    assert smtp.connected == 1
    assert smtp.logins == [("user", "secret")]
    assert smtp.message["To"] == "ops@example.com"
    assert delivered["external_reference"].startswith("<")
    assert delivered["metadata"] == {"accepted": True, "response": "queued"}

    for code, retryable in ((450, True), (550, False)):
        with pytest.raises(DeliveryError) as exc:
            await EmailDelivery(FakeSmtp(code), sender="watchdog@example.com", username="", password="").deliver(target)
        assert exc.value.retryable is retryable
