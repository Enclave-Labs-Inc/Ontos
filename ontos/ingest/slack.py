"""Slack connector — stub behind the `slack` extras.

The real Slack integration takes a bot token per-tenant, which is out
of scope for a runtime library shipping this milestone. This module
documents the shape the real impl will follow when it lands:

- Constructed from a bot token + optional channel filter.
- `iter_documents()` walks `conversations.history` for every channel
  the bot is a member of, yielding one `SourceDocument` per message
  (source_id = `slack:{team_id}:{channel_id}:{ts}` for stability
  across re-ingests; acl_ref set to `slack:channel:{channel_id}`
  so channel-level authz maps naturally to Ontos ACLs).

Calling `SlackConnector.from_token(...)` today raises
`NotImplementedError` with a clear pointer — better to fail loud than
to ship a half-connector that appears to work but drops half the
messages.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ontos.ingest.base import ConnectorError, SourceDocument


class SlackConnector:
    """Placeholder for the M4.b Slack connector."""

    id: str = "ontos.ingest.slack"
    source_kind: str = "slack"

    def __init__(self, bot_token: str, channels: list[str] | None = None) -> None:
        self._token = bot_token
        self._channels = channels or []

    @classmethod
    def from_token(cls, bot_token: str) -> SlackConnector:
        raise NotImplementedError(
            "SlackConnector is a placeholder in M4.b. Real Slack ingest "
            "lands as a follow-up (requires slack-sdk + per-tenant bot "
            "auth, both out of scope for the runtime library)."
        )

    async def iter_documents(self) -> AsyncIterator[SourceDocument]:
        raise ConnectorError(
            "SlackConnector.iter_documents is not implemented in this milestone"
        )
        yield  # pragma: no cover — keeps the type-checker happy about async iterator
