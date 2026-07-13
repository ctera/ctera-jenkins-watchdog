from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jenkins_watchdog import auth
from jenkins_watchdog.auth import OidcAuth
from jenkins_watchdog.config import Settings


def configured_auth(**overrides) -> OidcAuth:
    values = {
        "oidc_issuer": "https://issuer.test",
        "oidc_client_id": "watchdog",
        "oidc_client_secret": "secret",
        "oidc_redirect_uri": "https://watchdog.test/auth/callback",
        "oidc_allowed_groups": "DevOps Team, SRE",
    }
    values.update(overrides)
    return OidcAuth(Settings(**values))


def auth_app(service: OidcAuth) -> FastAPI:
    app = FastAPI()
    app.include_router(service.router)
    return app


def test_session_tokens_verify_and_reject_tampering_or_expiry(monkeypatch) -> None:
    service = configured_auth()
    token = service.create_session_token(
        {"email": "operator@example.com", "name": "Operator", "groups": ["DevOps Team"]}
    )

    assert service.verify_session_token(token)["email"] == "operator@example.com"
    assert service.verify_session_token(f"{token}tampered") is None
    assert service.verify_session_token("invalid") is None

    current_time = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: current_time + auth.SESSION_MAX_AGE + 1)
    assert service.verify_session_token(token) is None


def test_disabled_auth_me_and_login_are_local_guest() -> None:
    service = configured_auth(oidc_issuer="")
    client = TestClient(auth_app(service))

    me = client.get("/auth/me")
    login = client.get("/auth/login", follow_redirects=False)

    assert not service.enabled
    assert me.json() == {"authenticated": True, "email": "", "name": "Guest"}
    assert login.headers["location"] == "/"


def test_login_builds_oidc_redirect_and_state_cookie(monkeypatch) -> None:
    service = configured_auth()

    async def oidc_config():
        return {"authorization_endpoint": "https://issuer.test/authorize"}

    monkeypatch.setattr(service, "_get_oidc_config", oidc_config)
    response = TestClient(auth_app(service), base_url="https://watchdog.test").get(
        "/auth/login", follow_redirects=False
    )

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://issuer.test/authorize?")
    assert "client_id=watchdog" in response.headers["location"]
    assert "oidc_state=" in response.headers["set-cookie"]


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeAsyncClient:
    groups = ["DevOps Team"]
    token_status = 200
    userinfo_status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, data):
        del url, data
        return FakeResponse(self.token_status, {"access_token": "token"}, "token failed")

    async def get(self, url, headers):
        del url, headers
        return FakeResponse(
            self.userinfo_status,
            {"email": "operator@example.com", "name": "Operator", "groups": self.groups},
        )


def test_callback_creates_session_and_me_reads_it(monkeypatch) -> None:
    service = configured_auth()

    async def oidc_config():
        return {"token_endpoint": "https://issuer.test/token", "userinfo_endpoint": "https://issuer.test/user"}

    monkeypatch.setattr(service, "_get_oidc_config", oidc_config)
    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(auth_app(service), base_url="https://watchdog.test")
    client.cookies.set("oidc_state", "state")

    callback = client.get("/auth/callback?code=code&state=state", follow_redirects=False)
    me = client.get("/auth/me")
    logout = client.get("/auth/logout", follow_redirects=False)

    assert callback.status_code == 307 and callback.headers["location"] == "/"
    assert me.json()["email"] == "operator@example.com"
    assert logout.headers["location"] == "/"


def test_callback_rejects_state_exchange_userinfo_and_group(monkeypatch) -> None:
    service = configured_auth()

    async def oidc_config():
        return {"token_endpoint": "https://issuer.test/token", "userinfo_endpoint": "https://issuer.test/user"}

    monkeypatch.setattr(service, "_get_oidc_config", oidc_config)
    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(auth_app(service), base_url="https://watchdog.test")
    assert client.get("/auth/callback?code=code&state=wrong").status_code == 400

    client.cookies.set("oidc_state", "state")
    FakeAsyncClient.token_status = 500
    assert client.get("/auth/callback?code=code&state=state").status_code == 401

    FakeAsyncClient.token_status = 200
    FakeAsyncClient.userinfo_status = 500
    client.cookies.set("oidc_state", "state")
    assert client.get("/auth/callback?code=code&state=state").status_code == 401

    FakeAsyncClient.userinfo_status = 200
    FakeAsyncClient.groups = ["Guests"]
    client.cookies.set("oidc_state", "state")
    assert client.get("/auth/callback?code=code&state=state").status_code == 403

    FakeAsyncClient.groups = ["DevOps Team"]
