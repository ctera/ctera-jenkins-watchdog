"""OIDC authentication service with injected runtime settings."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from jenkins_watchdog.config import Settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "watchdog_session"
SESSION_MAX_AGE = 8 * 3600


class OidcAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._oidc_config: dict | None = None
        self.router = APIRouter(prefix="/auth", tags=["auth"])
        self.router.add_api_route("/login", self.login, methods=["GET"])
        self.router.add_api_route("/callback", self.callback, methods=["GET"])
        self.router.add_api_route("/me", self.me, methods=["GET"])
        self.router.add_api_route("/logout", self.logout, methods=["GET"])

    @property
    def enabled(self) -> bool:
        return bool(self.settings.oidc_issuer.strip())

    def _secret_key(self) -> bytes:
        return hashlib.sha256(self.settings.oidc_client_secret.encode()).digest()

    def create_session_token(self, user_info: dict) -> str:
        payload = {
            "email": user_info.get("email", ""),
            "name": user_info.get("name", ""),
            "groups": user_info.get("groups", []),
            "exp": int(time.time()) + SESSION_MAX_AGE,
        }
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        signature = hmac.new(self._secret_key(), payload_b64.encode(), hashlib.sha256).hexdigest()
        return f"{payload_b64}.{signature}"

    def verify_session_token(self, token: str) -> dict | None:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, signature = parts
        expected = hmac.new(self._secret_key(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception:
            return None
        if payload.get("exp", 0) < time.time():
            return None
        return payload

    async def _get_oidc_config(self) -> dict:
        if self._oidc_config is None:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.settings.oidc_issuer}/.well-known/openid-configuration")
                response.raise_for_status()
                self._oidc_config = response.json()
        return self._oidc_config

    def _allowed_groups(self) -> set[str]:
        return {group.strip() for group in self.settings.oidc_allowed_groups.split(",") if group.strip()}

    async def login(self):
        if not self.enabled:
            return RedirectResponse(url="/")
        oidc = await self._get_oidc_config()
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": self.settings.oidc_client_id,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "response_type": "code",
            "scope": "openid profile email groups",
            "state": state,
        }
        response = RedirectResponse(url=f"{oidc['authorization_endpoint']}?{urlencode(params)}")
        response.set_cookie("oidc_state", state, httponly=True, secure=True, max_age=300, samesite="lax")
        return response

    async def callback(self, request: Request, code: str = "", state: str = ""):
        stored_state = request.cookies.get("oidc_state")
        if not stored_state or stored_state != state:
            return JSONResponse({"error": "Invalid state"}, status_code=400)
        oidc = await self._get_oidc_config()
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                oidc["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.settings.oidc_client_id,
                    "client_secret": self.settings.oidc_client_secret,
                    "code": code,
                    "redirect_uri": self.settings.oidc_redirect_uri,
                },
            )
        if token_response.status_code != 200:
            logger.error("Token exchange failed: %s", token_response.text)
            return JSONResponse({"error": "Authentication failed"}, status_code=401)
        async with httpx.AsyncClient() as client:
            userinfo_response = await client.get(
                oidc["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
            )
        if userinfo_response.status_code != 200:
            return JSONResponse({"error": "Failed to get user info"}, status_code=401)
        user_info = userinfo_response.json()
        if not set(user_info.get("groups", [])) & self._allowed_groups():
            logger.warning("User %s denied by OIDC group policy", user_info.get("email"))
            return JSONResponse({"error": "Access denied. Required group membership."}, status_code=403)
        response = RedirectResponse(url="/")
        response.set_cookie(
            SESSION_COOKIE,
            self.create_session_token(user_info),
            httponly=True,
            max_age=SESSION_MAX_AGE,
            samesite="lax",
            secure=True,
        )
        response.delete_cookie("oidc_state")
        return response

    async def me(self, request: Request):
        if not self.enabled:
            return {"authenticated": True, "email": "", "name": "Guest"}
        session = request.cookies.get(SESSION_COOKIE)
        payload = self.verify_session_token(session) if session else None
        if payload is None:
            return JSONResponse({"authenticated": False}, status_code=401)
        return {"authenticated": True, "email": payload["email"], "name": payload["name"]}

    async def logout(self):
        response = RedirectResponse(url="/")
        response.delete_cookie(SESSION_COOKIE)
        return response

    def require_auth(self, request: Request) -> dict | None:
        session = request.cookies.get(SESSION_COOKIE)
        return self.verify_session_token(session) if session else None
