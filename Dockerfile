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

COPY src/ ./src/
COPY prompts/ ./prompts/
COPY frontend/dist ./frontend/dist

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "-m", "jenkins_watchdog"]
