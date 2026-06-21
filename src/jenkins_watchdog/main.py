"""FastAPI application — on-demand scan, findings API, and SPA serving."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from jenkins_watchdog.auth import require_auth
from jenkins_watchdog.auth import router as auth_router
from jenkins_watchdog.clients.prometheus import close_prometheus_client
from jenkins_watchdog.clients.valkey import close_valkey_client
from jenkins_watchdog.config import settings

logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")

FRONTEND_DIR = Path(os.environ.get("WATCHDOG_FRONTEND_DIR", "/app/frontend/dist"))

PUBLIC_PATHS = {"/health", "/ready", "/auth/login", "/auth/callback", "/auth/logout"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_valkey_client()
    await close_prometheus_client()


app = FastAPI(
    title="Jenkins Watchdog",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in PUBLIC_PATHS):
        return await call_next(request)
    if not settings.oidc_client_secret:
        return await call_next(request)
    user = require_auth(request)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return RedirectResponse(url="/auth/login")
    request.state.user = user
    return await call_next(request)


app.include_router(auth_router)

from jenkins_watchdog.api.chat import router as chat_router  # noqa: E402
from jenkins_watchdog.api.jira import router as jira_router  # noqa: E402
from jenkins_watchdog.api.router import router  # noqa: E402

app.include_router(router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(jira_router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    from jenkins_watchdog.clients.valkey import get_valkey_client
    try:
        client = await get_valkey_client()
        await client.ping()
    except Exception:
        return JSONResponse({"status": "not ready", "reason": "valkey unreachable"}, status_code=503)
    return {"status": "ready"}


if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="static")

    @app.get("/{path:path}")
    async def serve_spa(path: str):
        file_path = FRONTEND_DIR / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
