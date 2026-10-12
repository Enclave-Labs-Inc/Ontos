"""Parsing for caller-supplied bitemporal `as_of` timestamps."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_as_of(value: str) -> datetime:
    """Parse an ISO-8601 ``as_of`` timestamp, treating offset-less values as UTC.

    Stored ``t_valid`` / ``t_invalid`` / ``resolved_at`` are always
    timezone-aware. A naive value would raise ``TypeError`` against the
    NetworkX store and silently match nothing against Neo4j (``LocalDateTime``
    vs ``DateTime`` compares to null), so every entry point normalizes here.

    Raises ``ValueError`` when ``value`` is not ISO-8601.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
