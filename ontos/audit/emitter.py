"""Hash-chained Article-12-compliant audit log.

Every AI-influenced decision produces one AuditRecord. Records are append-only,
each linking to the previous by content hash. Verifiers can walk the chain and
detect any insertion, deletion, or reordering.

The signing key is loaded from an env var (name held in RuntimeConfig).
In production, that env var is populated by KMS decrypt at boot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from ontos.runtime.models import ArticleTwelveField, AuditRecord


class AuditEmitter:
    """Append-only audit sink. In M0 this holds records in memory; M2 persists
    to Postgres and adds retention enforcement (≥6 months per Article 12)."""

    def __init__(self, signing_key: bytes) -> None:
        if not signing_key:
            raise ValueError("audit signing key is required — never boot without it")
        self._key = signing_key
        self._chain: list[AuditRecord] = []

    @classmethod
    def from_env(cls, env_var: str) -> AuditEmitter:
        raw = os.environ.get(env_var)
        if not raw:
            raise RuntimeError(
                f"{env_var} not set — audit emitter refuses to boot without a signing key"
            )
        return cls(signing_key=raw.encode("utf-8"))

    def emit(
        self,
        *,
        query_id: UUID | None,
        agent_identity: str,
        acting_on_behalf_of: str | None,
        query_text: str,
        tool_invoked: str,
        tool_arguments: dict[str, object],
        result_fact_ids: list[str],
        latency_ms: float,
        model_versions: dict[str, str],
        policy_decisions: list[dict[str, str]],
    ) -> AuditRecord:
        qid = query_id or uuid4()
        result_hash = hashlib.sha256(
            json.dumps(sorted(result_fact_ids)).encode("utf-8")
        ).hexdigest()
        article12: dict[ArticleTwelveField, str] = {
            ArticleTwelveField.QUERY_ID: str(qid),
            ArticleTwelveField.AGENT_IDENTITY: agent_identity,
            ArticleTwelveField.ACTING_ON_BEHALF_OF: acting_on_behalf_of or "",
            ArticleTwelveField.QUERY_TEXT: query_text,
            ArticleTwelveField.TOOL_INVOKED: tool_invoked,
            ArticleTwelveField.TOOL_ARGUMENTS: json.dumps(tool_arguments, sort_keys=True),
            ArticleTwelveField.RESULT_FACT_IDS: json.dumps(sorted(result_fact_ids)),
            ArticleTwelveField.RESULT_HASH: result_hash,
            ArticleTwelveField.TIMESTAMP: datetime.now(UTC).isoformat(),
            ArticleTwelveField.LATENCY_MS: f"{latency_ms:.3f}",
            ArticleTwelveField.MODEL_VERSIONS: json.dumps(model_versions, sort_keys=True),
            ArticleTwelveField.POLICY_DECISIONS: json.dumps(policy_decisions, sort_keys=True),
        }
        prev_hash = self._chain[-1].hash if self._chain else None
        payload = json.dumps(
            {"article12": {k.value: v for k, v in article12.items()}, "prev_hash": prev_hash},
            sort_keys=True,
        ).encode("utf-8")
        record_hash = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
        record = AuditRecord(
            query_id=qid,
            timestamp=datetime.now(UTC),
            article12=article12,
            prev_hash=prev_hash,
            hash=record_hash,
        )
        self._chain.append(record)
        return record

    def by_query_id(self, query_id: UUID) -> AuditRecord | None:
        for record in reversed(self._chain):
            if record.query_id == query_id:
                return record
        return None

    def verify_chain(self) -> bool:
        prev_hash: str | None = None
        for record in self._chain:
            expected_payload = json.dumps(
                {
                    "article12": {k.value: v for k, v in record.article12.items()},
                    "prev_hash": prev_hash,
                },
                sort_keys=True,
            ).encode("utf-8")
            expected_hash = hmac.new(self._key, expected_payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected_hash, record.hash):
                return False
            if record.prev_hash != prev_hash:
                return False
            prev_hash = record.hash
        return True

    def __len__(self) -> int:
        return len(self._chain)
