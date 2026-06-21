"""OIDC authentication via DEX — login, callback, session cookie, middleware."""

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

from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_oidc_config: dict | None = None

SESSION_COOKIE = "watchdog_session"
SESSION_MAX_AGE = 8 * 3600


def is_auth_enabled() -> bool:
    """Return True when OIDC is configured (issuer set)."""
    return bool(settings.oidc_issuer.strip())


def _get_secret_key() -> bytes:
    """Derive a signing key from the OIDC client secret."""
    return hashlib.sha256(settings.oidc_client_secret.encode()).digest()


def _create_session_token(user_info: dict) -> str:
    """Create a signed session token (base64 JSON + HMAC)."""
    import base64

    payload = {
        "email": user_info.get("email", ""),
        "name": user_info.get("name", ""),
        "groups": user_info.get("groups", []),
        "exp": int(time.time()) + SESSION_MAX_AGE,
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(_get_secret_key(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def _verify_session_token(token: str) -> dict | None:
    """Verify and decode a session token. Returns None if invalid/expired."""
    import base64

    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig = parts
    expected_sig = hmac.new(_get_secret_key(), payload_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


async def _get_oidc_config() -> dict:
    """Fetch and cache the OIDC discovery document."""
    global _oidc_config
    if _oidc_config is None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.oidc_issuer}/.well-known/openid-configuration")
            resp.raise_for_status()
            _oidc_config = resp.json()
    return _oidc_config


def _get_allowed_groups() -> set[str]:
    return {g.strip() for g in settings.oidc_allowed_groups.split(",") if g.strip()}


@router.get("/login")
async def login():
    """Redirect to DEX authorization endpoint."""
    if not is_auth_enabled():
        return RedirectResponse(url="/")
    oidc = await _get_oidc_config()
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "response_type": "code",
        "scope": "openid profile email groups",
        "state": state,
    }
    url = f"{oidc['authorization_endpoint']}?{urlencode(params)}"
    response = RedirectResponse(url=url)
    response.set_cookie("oidc_state", state, httponly=True, secure=True, max_age=300, samesite="lax")
    return response


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    """Handle OIDC callback — exchange code for tokens, create session."""
    stored_state = request.cookies.get("oidc_state")
    if not stored_state or stored_state != state:
        return JSONResponse({"error": "Invalid state"}, status_code=400)

    oidc = await _get_oidc_config()

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            oidc["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
                "code": code,
                "redirect_uri": settings.oidc_redirect_uri,
            },
        )

    if token_resp.status_code != 200:
        logger.error("Token exchange failed: %s", token_resp.text)
        return JSONResponse({"error": "Authentication failed"}, status_code=401)

    tokens = token_resp.json()

    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            oidc["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

    if userinfo_resp.status_code != 200:
        return JSONResponse({"error": "Failed to get user info"}, status_code=401)

    user_info = userinfo_resp.json()
    user_groups = set(user_info.get("groups", []))
    allowed = _get_allowed_groups()

    if not user_groups & allowed:
        logger.warning("User %s denied — groups %s not in %s", user_info.get("email"), user_groups, allowed)
        return JSONResponse({"error": "Access denied. Required group membership."}, status_code=403)

    session_token = _create_session_token(user_info)
    response = RedirectResponse(url="/")
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        httponly=True,
        max_age=SESSION_MAX_AGE,
        samesite="lax",
        secure=True,
    )
    response.delete_cookie("oidc_state")
    return response


@router.get("/me")
async def me(request: Request):
    """Return current user info if authenticated."""
    if not is_auth_enabled():
        return {"authenticated": True, "email": "", "name": "Guest"}
    session = request.cookies.get(SESSION_COOKIE)
    if not session:
        return JSONResponse({"authenticated": False}, status_code=401)
    payload = _verify_session_token(session)
    if not payload:
        return JSONResponse({"authenticated": False}, status_code=401)
    return {"authenticated": True, "email": payload["email"], "name": payload["name"]}


@router.get("/logout")
async def logout():
    """Clear session cookie."""
    response = RedirectResponse(url="/")
    response.delete_cookie(SESSION_COOKIE)
    return response


def require_auth(request: Request) -> dict | None:
    """Check if request is authenticated. Returns user payload or None."""
    session = request.cookies.get(SESSION_COOKIE)
    if not session:
        return None
    return _verify_session_token(session)
