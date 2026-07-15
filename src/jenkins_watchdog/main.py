"""FastAPI application for the durable v2 API and SPA."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from jenkins_watchdog.auth import OidcAuth
from jenkins_watchdog.bootstrap import build_container, load_settings
from jenkins_watchdog.entrypoints.api_v2 import router as api_v2_router

settings = load_settings()
authentication = OidcAuth(settings)
logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")

FRONTEND_DIR = Path(os.environ.get("WATCHDOG_FRONTEND_DIR", "/app/frontend/dist"))

PUBLIC_PATHS = {"/health", "/ready", "/auth/login", "/auth/callback", "/auth/logout"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    container = build_container(settings)
    app.state.container = container
    try:
        yield
    finally:
        await container.close()


app = FastAPI(
    title="Jenkins Watchdog",
    version="2.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in PUBLIC_PATHS):
        return await call_next(request)
    if not authentication.enabled:
        if settings.local_actor_email:
            request.state.user = {"email": settings.local_actor_email, "name": "Local operator"}
        return await call_next(request)
    user = authentication.require_auth(request)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return RedirectResponse(url="/auth/login")
    request.state.user = user
    return await call_next(request)


app.include_router(authentication.router)
app.include_router(api_v2_router, prefix="/api/v2")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    try:
        await app.state.container.ready()
    except Exception as exc:
        return JSONResponse({"status": "not ready", "reason": type(exc).__name__}, status_code=503)
    return {"status": "ready"}


if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="static")

    @app.get("/{path:path}")
    async def serve_spa(path: str):
        file_path = FRONTEND_DIR / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
