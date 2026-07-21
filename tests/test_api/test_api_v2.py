from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jenkins_watchdog.application.scan_service import ScanService
from jenkins_watchdog.infrastructure.events import PollingEventNotifier
from jenkins_watchdog.infrastructure.memory import InMemoryUnitOfWorkFactory
from jenkins_watchdog.main import app


@pytest.fixture(autouse=True)
def reset_v2_scan_service():
    factory = InMemoryUnitOfWorkFactory()
    app.state.container = SimpleNamespace(
        scan_service=ScanService(factory),
        uow_factory=factory,
        notifier=PollingEventNotifier(),
    )


def test_v2_enqueue_scan_returns_accepted():
    client = TestClient(app)

    response = client.post("/api/v2/scans", json={"mode": "regular", "categories": ["k8s_node"]})

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["mode"] == "regular"
    assert payload["categories"] == ["k8s_node"]
    assert payload["urls"]["events"].endswith("/events")


def test_v2_enqueue_rejects_active_scan():
    client = TestClient(app)
    first = client.post("/api/v2/scans", json={"mode": "deep"}).json()

    response = client.post("/api/v2/scans", json={"mode": "regular"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "scan_active"
    assert detail["active_scan"]["id"] == first["id"]


def test_v2_enqueue_validates_categories():
    client = TestClient(app)

    response = client.post("/api/v2/scans", json={"categories": ["not-real"]})

    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "unknown_categories", "categories": ["not-real"]}
