"""Valkey client with optional mTLS support."""

import logging
from pathlib import Path

import valkey.asyncio as valkey

from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)
_client: valkey.Valkey | None = None


async def get_valkey_client() -> valkey.Valkey:
    global _client
    if _client is None:
        use_ssl = settings.valkey_ssl

        if use_ssl and not Path(settings.valkey_ca_cert).exists():
            logger.warning("Valkey TLS certs not found, falling back to non-TLS")
            use_ssl = False

        ssl_kwargs = {}
        if use_ssl:
            ssl_kwargs["ssl"] = True
            if settings.valkey_ca_cert:
                ssl_kwargs["ssl_ca_certs"] = settings.valkey_ca_cert
            if settings.valkey_client_cert:
                ssl_kwargs["ssl_certfile"] = settings.valkey_client_cert
            if settings.valkey_client_key:
                ssl_kwargs["ssl_keyfile"] = settings.valkey_client_key

        _client = valkey.Valkey(
            host=settings.valkey_host,
            port=settings.valkey_port,
            decode_responses=True,
            socket_timeout=10,
            retry_on_timeout=True,
            **ssl_kwargs,
        )
    return _client


async def close_valkey_client() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None
