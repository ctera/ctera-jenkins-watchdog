"""Explicit lazy Kubernetes client used by detector adapters."""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from kubernetes import config
from kubernetes.client import AppsV1Api, BatchV1Api, CoreV1Api, CustomObjectsApi

logger = logging.getLogger(__name__)
T = TypeVar("T")


class KubernetesClient:
    def __init__(self, *, request_timeout_seconds: float) -> None:
        self.request_timeout_seconds = request_timeout_seconds
        self._core_v1: CoreV1Api | None = None
        self._apps_v1: AppsV1Api | None = None
        self._batch_v1: BatchV1Api | None = None
        self._custom: CustomObjectsApi | None = None

    def _initialize(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
            logger.warning("Using local kubeconfig (not in-cluster)")
        self._core_v1 = CoreV1Api()
        self._apps_v1 = AppsV1Api()
        self._batch_v1 = BatchV1Api()
        self._custom = CustomObjectsApi()

    def core_v1(self) -> CoreV1Api:
        if self._core_v1 is None:
            self._initialize()
        assert self._core_v1 is not None
        return self._core_v1

    def apps_v1(self) -> AppsV1Api:
        if self._apps_v1 is None:
            self._initialize()
        assert self._apps_v1 is not None
        return self._apps_v1

    def batch_v1(self) -> BatchV1Api:
        if self._batch_v1 is None:
            self._initialize()
        assert self._batch_v1 is not None
        return self._batch_v1

    def custom_objects(self) -> CustomObjectsApi:
        if self._custom is None:
            self._initialize()
        assert self._custom is not None
        return self._custom

    async def run_sync(
        self,
        func: Callable[..., T],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> T:
        call = functools.partial(func, *args, **kwargs)
        call_timeout = timeout if timeout is not None else self.request_timeout_seconds
        try:
            return await asyncio.wait_for(asyncio.to_thread(call), timeout=call_timeout)
        except TimeoutError:
            raise TimeoutError(f"Kubernetes API call timed out after {call_timeout}s") from None

    def close(self) -> None:
        clients = {
            id(api.api_client): api.api_client
            for api in (self._core_v1, self._apps_v1, self._batch_v1, self._custom)
            if api is not None
        }
        for client in clients.values():
            client.close()
