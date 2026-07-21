from datetime import datetime, timezone

import pytest

from jenkins_watchdog.application.pagination import InvalidCursorError, decode_cursor, encode_cursor


def test_cursor_round_trip_is_opaque_and_stable() -> None:
    created_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    cursor = encode_cursor(created_at, "12345678-1234-5678-1234-567812345678")

    assert "2026" not in cursor
    assert decode_cursor(cursor) == (created_at, "12345678-1234-5678-1234-567812345678")


@pytest.mark.parametrize("cursor", ["", "not-base64", "W10", "eyJ0IjoxLCJpIjpudWxsfQ"])
def test_cursor_rejects_malformed_values(cursor: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)
