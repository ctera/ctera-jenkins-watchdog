FROM python:3.12-slim AS builder

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml .
RUN mkdir -p src/jenkins_watchdog && \
    touch src/jenkins_watchdog/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf src/

COPY src/ ./src/
RUN uv pip install --system --no-cache --no-deps .

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# The agent spawns the `claude` CLI per call. No node/npm is needed: the claude-agent-sdk
# wheel bundles the binary and the SDK prefers it over $PATH. Fail the build if it is
# missing or unrunnable — installing from an sdist (no wheel for the platform) silently
# yields an SDK with no binary, and that would otherwise surface as every investigation
# dying at runtime.
RUN /usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude --version

COPY src/ ./src/
COPY prompts/ ./prompts/
COPY frontend/dist ./frontend/dist

ENV PYTHONUNBUFFERED=1 \
    HOME=/home/watchdog \
    WATCHDOG_CLAUDE_CONFIG_DIR=/home/watchdog/.claude-home \
    WATCHDOG_PROMPTS_DIR=/app/prompts \
    DISABLE_AUTOUPDATER=1
# The CLI writes session state under its config dir, so it must be writable. It must also
# stay EMPTY of credentials: that emptiness is what forces the OAuth token to be the only
# way in, so a revoked token fails loud instead of resolving some other identity.
# DISABLE_AUTOUPDATER keeps the pinned CLI pinned.
RUN mkdir -p /home/watchdog/.claude-home && chmod 0700 /home/watchdog/.claude-home
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "-m", "jenkins_watchdog"]
