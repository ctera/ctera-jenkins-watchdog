from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from jenkins_watchdog.domain.serialization import to_primitive
from jenkins_watchdog.infrastructure.mappers import jsonable


def test_nested_operational_values_are_json_safe() -> None:
    value = {
        "observed_at": datetime(2026, 7, 15, 13, 4, tzinfo=timezone.utc),
        "day": date(2026, 7, 15),
        "cost": Decimal("0.00123456"),
        "id": UUID("23de7609-330d-4de0-84da-beffc15be54f"),
        "nested": ({"at": datetime(2026, 7, 15, tzinfo=timezone.utc)},),
    }

    expected = {
        "observed_at": "2026-07-15T13:04:00+00:00",
        "day": "2026-07-15",
        "cost": 0.00123456,
        "id": "23de7609-330d-4de0-84da-beffc15be54f",
        "nested": [{"at": "2026-07-15T00:00:00+00:00"}],
    }
    assert to_primitive(value) == expected
    assert jsonable(value) == expected
