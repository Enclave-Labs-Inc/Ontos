"""Unit tests for `ontos.runtime.temporal.parse_as_of` (#60)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ontos.runtime.temporal import parse_as_of


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-06-01", datetime(2026, 6, 1, tzinfo=UTC)),
        ("2026-06-01T12:30:00", datetime(2026, 6, 1, 12, 30, tzinfo=UTC)),
        ("2026-06-01T12:30:00Z", datetime(2026, 6, 1, 12, 30, tzinfo=UTC)),
    ],
)
def test_parse_as_of_treats_offsetless_values_as_utc(value: str, expected: datetime) -> None:
    parsed = parse_as_of(value)

    assert parsed == expected
    assert parsed.tzinfo is not None


def test_parse_as_of_preserves_explicit_offset() -> None:
    parsed = parse_as_of("2026-06-01T12:00:00+02:00")

    assert parsed.utcoffset() == timedelta(hours=2)
    assert parsed == datetime(2026, 6, 1, 12, tzinfo=timezone(timedelta(hours=2)))


def test_parse_as_of_rejects_non_iso_values() -> None:
    with pytest.raises(ValueError):
        parse_as_of("last tuesday")
