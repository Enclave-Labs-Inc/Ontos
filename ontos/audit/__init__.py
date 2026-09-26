"""audit subsystem for ontos.

Two AuditSink implementations ship:

- `AuditEmitter` — in-memory hash-chained log. Dev + tests only; does
  not survive restart.
- `PostgresAuditEmitter` — persistent, retention-aware. Required for
  regulated deploys (Article 12 wants ≥6 months of retention).

Both satisfy the `AuditSink` structural contract; `build_server()`
accepts either.
"""

from ontos.audit.base import AuditSink
from ontos.audit.emitter import AuditEmitter
from ontos.audit.postgres import PostgresAuditEmitter

__all__ = [
    "AuditEmitter",
    "AuditSink",
    "PostgresAuditEmitter",
]
