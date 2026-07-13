"""Jira, SMTP, GitHub, and GitLab delivery adapters."""

from __future__ import annotations

import hashlib
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

import httpx
from aiosmtplib import SMTP
from aiosmtplib.errors import SMTPException, SMTPResponseException

from jenkins_watchdog.application.delivery import DeliveryError
from jenkins_watchdog.domain.model import Action, ActionType
from jenkins_watchdog.domain.serialization import to_primitive


class DeliveryRouter:
    def __init__(
        self,
        *,
        jira: JiraDelivery | None,
        email: EmailDelivery | None,
        github: GitHubDelivery | None,
        gitlab: GitLabDelivery | None,
    ) -> None:
        self._adapters = {
            ActionType.JIRA_CREATE: jira,
            ActionType.JIRA_UPDATE: jira,
            ActionType.EMAIL: email,
            ActionType.GITHUB_COMMENT: github,
            ActionType.GITLAB_COMMENT: gitlab,
        }

    async def deliver(self, action: Action) -> dict[str, Any]:
        adapter = self._adapters[action.action_type]
        if adapter is None:
            raise DeliveryError("integration is disabled", retryable=False)
        return await adapter.deliver(action)


class JiraDelivery:
    def __init__(self, client: httpx.AsyncClient, *, base_url: str, user: str, token: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._auth = (user, token)

    async def deliver(self, action: Action) -> dict[str, Any]:
        try:
            existing = await self._find_existing(action)
            if action.action_type is ActionType.JIRA_CREATE and existing:
                return {"external_reference": existing, "metadata": {"reused": True}}
            payload = to_primitive(action.rendered_payload)
            if action.action_type is ActionType.JIRA_UPDATE:
                if not existing:
                    raise DeliveryError("Jira incident issue was not found", retryable=False)
                response = await self._client.post(
                    f"{self._base_url}/rest/api/3/issue/{existing}/comment",
                    auth=self._auth,
                    json={"body": _adf(str(payload["description"]))},
                )
                _raise_http(response)
                return {"external_reference": existing, "metadata": _http_metadata(response)}
            label = _jira_label(action)
            response = await self._client.post(
                f"{self._base_url}/rest/api/3/issue",
                auth=self._auth,
                json={
                    "fields": {
                        "project": {"key": action.destination},
                        "summary": payload["summary"],
                        "description": _adf(str(payload["description"])),
                        "issuetype": {"name": "Task"},
                        "labels": ["jenkins-watchdog", label],
                    }
                },
            )
            _raise_http(response)
            key = response.json().get("key")
            if not key:
                raise DeliveryError("Jira response did not include an issue key", retryable=True)
            return {"external_reference": str(key), "metadata": _http_metadata(response)}
        except httpx.RequestError as exc:
            raise DeliveryError(type(exc).__name__, retryable=True) from exc

    async def _find_existing(self, action: Action) -> str | None:
        response = await self._client.get(
            f"{self._base_url}/rest/api/3/search/jql",
            auth=self._auth,
            params={"jql": f'labels = "{_jira_label(action)}"', "maxResults": 1, "fields": "key"},
        )
        _raise_http(response)
        issues = response.json().get("issues", [])
        return str(issues[0]["key"]) if issues else None


class GitHubDelivery:
    def __init__(self, client: httpx.AsyncClient, *, api_url: str, token: str) -> None:
        self._client = client
        self._api_url = api_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    async def deliver(self, action: Action) -> dict[str, Any]:
        _, repository, change = action.destination.split(":", 2)
        url = f"{self._api_url}/repos/{repository}/issues/{change}/comments"
        marker = f"<!-- watchdog:{action.idempotency_key} -->"
        try:
            existing = await self._client.get(url, headers=self._headers, params={"per_page": 100})
            _raise_http(existing)
            for comment in existing.json():
                if marker in str(comment.get("body", "")):
                    return {
                        "external_reference": str(comment.get("html_url") or comment.get("id")),
                        "metadata": {"reused": True},
                    }
            body = f"{action.rendered_payload['body']}\n\n{marker}"
            response = await self._client.post(url, headers=self._headers, json={"body": body})
            _raise_http(response)
            value = response.json()
            return {
                "external_reference": str(value.get("html_url") or value.get("id")),
                "metadata": _http_metadata(response),
            }
        except httpx.RequestError as exc:
            raise DeliveryError(type(exc).__name__, retryable=True) from exc


class GitLabDelivery:
    def __init__(self, client: httpx.AsyncClient, *, api_url: str, token: str) -> None:
        self._client = client
        self._api_url = api_url.rstrip("/")
        self._headers = {"PRIVATE-TOKEN": token}

    async def deliver(self, action: Action) -> dict[str, Any]:
        _, repository, change = action.destination.split(":", 2)
        url = f"{self._api_url}/projects/{quote(repository, safe='')}/merge_requests/{change}/notes"
        marker = f"<!-- watchdog:{action.idempotency_key} -->"
        try:
            existing = await self._client.get(url, headers=self._headers, params={"per_page": 100})
            _raise_http(existing)
            for note in existing.json():
                if marker in str(note.get("body", "")):
                    return {"external_reference": str(note.get("id")), "metadata": {"reused": True}}
            response = await self._client.post(
                url,
                headers=self._headers,
                json={"body": f"{action.rendered_payload['body']}\n\n{marker}"},
            )
            _raise_http(response)
            return {
                "external_reference": str(response.json().get("id")),
                "metadata": _http_metadata(response),
            }
        except httpx.RequestError as exc:
            raise DeliveryError(type(exc).__name__, retryable=True) from exc


class EmailDelivery:
    def __init__(
        self,
        smtp: SMTP,
        *,
        sender: str,
        username: str,
        password: str,
    ) -> None:
        self._smtp = smtp
        self._sender = sender
        self._username = username
        self._password = password

    async def deliver(self, action: Action) -> dict[str, Any]:
        payload = action.rendered_payload
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = action.destination
        message["Subject"] = str(payload["subject"])
        digest = hashlib.sha256(action.idempotency_key.encode()).hexdigest()[:32]
        message["Message-ID"] = f"<{digest}@jenkins-watchdog>"
        message.set_content(str(payload["body"]))
        try:
            if not self._smtp.is_connected:
                await self._smtp.connect()
                if self._username:
                    await self._smtp.login(self._username, self._password)
            errors, response = await self._smtp.send_message(message)
            if errors:
                code = next(iter(errors.values())).code
                raise DeliveryError(f"SMTP {code}", retryable=400 <= code < 500)
            return {
                "external_reference": message["Message-ID"],
                "metadata": {"accepted": True, "response": str(response)[:120]},
            }
        except SMTPResponseException as exc:
            raise DeliveryError(f"SMTP {exc.code}", retryable=400 <= exc.code < 500) from exc
        except SMTPException as exc:
            raise DeliveryError(type(exc).__name__, retryable=True) from exc


def _raise_http(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    raise DeliveryError(
        f"HTTP {response.status_code}",
        retryable=response.status_code == 429 or response.status_code >= 500,
        metadata=_http_metadata(response),
    )


def _http_metadata(response: httpx.Response) -> dict[str, Any]:
    metadata: dict[str, Any] = {"status_code": response.status_code}
    request_id = response.headers.get("x-request-id") or response.headers.get("x-github-request-id")
    if request_id:
        metadata["request_id"] = request_id[:160]
    return metadata


def _jira_label(action: Action) -> str:
    incident_id = action.external_identity.rsplit(":", 1)[-1]
    return f"watchdog-incident-{incident_id}"[:255]


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }
