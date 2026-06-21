"""Kubernetes client using in-cluster config."""

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import TypeVar

from kubernetes import config
from kubernetes.client import (
    AppsV1Api,
    BatchV1Api,
    CoreV1Api,
    CustomObjectsApi,
)

logger = logging.getLogger(__name__)

_core_v1: CoreV1Api | None = None
_apps_v1: AppsV1Api | None = None
_batch_v1: BatchV1Api | None = None
_custom: CustomObjectsApi | None = None

T = TypeVar("T")


def _init_client() -> None:
    global _core_v1, _apps_v1, _batch_v1, _custom
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
        logger.warning("Using local kubeconfig (not in-cluster)")

    _core_v1 = CoreV1Api()
    _apps_v1 = AppsV1Api()
    _batch_v1 = BatchV1Api()
    _custom = CustomObjectsApi()


def get_core_v1() -> CoreV1Api:
    if _core_v1 is None:
        _init_client()
    return _core_v1


def get_apps_v1() -> AppsV1Api:
    if _apps_v1 is None:
        _init_client()
    return _apps_v1


def get_batch_v1() -> BatchV1Api:
    if _batch_v1 is None:
        _init_client()
    return _batch_v1


def get_custom() -> CustomObjectsApi:
    if _custom is None:
        _init_client()
    return _custom


async def run_sync(func: Callable[..., T], *args, **kwargs) -> T:
    """Run a synchronous K8s API call in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(functools.partial(func, *args, **kwargs))
