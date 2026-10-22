"""#32 — graph export must not leak ACL-restricted facts by default."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ontos.runtime.server
from ontos.authz import InMemoryAuthz
from ontos.cli.main import app
from ontos.runtime.models import Confidence, Fact, Provenance
from ontos.storage.networkx_store import NetworkxStore

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})


@pytest.fixture(autouse=True)
def _isolated_audit_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ONTOS_AUDIT_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}",
    )
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "test-signing-key-not-for-prod")


def _capture_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    audit_calls: list[dict[str, object]] = []

    class FakeAuditEmitter:
        async def initialize(self) -> None:
            return None

        async def emit_async(self, **kwargs: object) -> None:
            audit_calls.append(dict(kwargs))

        async def close(self) -> None:
            return None

    fake_audit = FakeAuditEmitter()
    monkeypatch.setattr(
        "ontos.audit.postgres.PostgresAuditEmitter.from_url",
        lambda *args, **kwargs: fake_audit,
    )
    return audit_calls


def _fact(
    subject: str,
    predicate: str,
    object_: str,
    *,
    acl_ref: str | None = None,
) -> Fact:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    return Fact(
        subject_id=subject,
        predicate=predicate,
        object_id=object_,
        provenance=Provenance(
            source_id="doc:test",
            extractor_id="extractor:test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.4",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=when,
        ingested_at=when,
        acl_ref=acl_ref,
    )


def _write_store(path: Path) -> tuple[Fact, Fact]:
    store = NetworkxStore(path=path)

    public = _fact(
        "company:acme",
        "has_public_profile",
        "profile:public",
    )
    restricted = _fact(
        "company:acme",
        "has_finance_record",
        "record:finance",
        acl_ref="acl:finance",
    )

    asyncio.run(store.add_fact(public))
    asyncio.run(store.add_fact(restricted))
    asyncio.run(store.close())

    return public, restricted


def test_default_export_drops_restricted_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "default.graphml"

    public, restricted = _write_store(store_path)
    audit_calls = _capture_audit(monkeypatch)

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr

    exported = output_path.read_text(encoding="utf-8")
    assert str(public.id) in exported
    assert str(restricted.id) not in exported

    assert len(audit_calls) == 1
    audit = audit_calls[0]
    assert audit["agent_identity"] == "ontos-cli"
    assert audit["acting_on_behalf_of"] is None
    assert audit["tool_invoked"] == "ontos.export.graphml"

    tool_arguments = audit["tool_arguments"]
    assert isinstance(tool_arguments, dict)
    assert tool_arguments["include_acl_mask"] is False
    assert tool_arguments["fact_count"] == 1
    assert tool_arguments["fact_count_by_acl"] == {"public": 1}

    assert audit["policy_decisions"] == []

    result_fact_ids = audit["result_fact_ids"]
    assert isinstance(result_fact_ids, list)
    assert result_fact_ids == [str(public.id)]
    assert str(restricted.id) not in result_fact_ids


def test_acl_mask_without_audit_role_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "denied.graphml"
    _write_store(store_path)

    authz = InMemoryAuthz()
    monkeypatch.setattr(
        ontos.runtime.server,
        "build_authz",
        lambda _cfg: authz,
    )
    audit_calls = _capture_audit(monkeypatch)

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
            "--acting-on-behalf-of",
            "user:bob",
            "--include-acl-mask",
        ],
    )

    assert result.exit_code == 2
    output = (result.stdout + result.stderr).lower()
    assert "audit_exporter" in output
    assert not output_path.exists()

    assert len(audit_calls) == 1
    audit = audit_calls[0]
    assert audit["acting_on_behalf_of"] == "user:bob"
    assert audit["agent_identity"] == "ontos-cli"
    assert audit["tool_invoked"] == "ontos.export.graphml"

    tool_arguments = audit["tool_arguments"]
    assert isinstance(tool_arguments, dict)
    assert tool_arguments["include_acl_mask"] is True
    assert tool_arguments["fact_count"] == 0
    assert tool_arguments["fact_count_by_acl"] == {}

    assert audit["policy_decisions"] == [
        {
            "relation": "audit_exporter",
            "object": "export",
            "decision": "deny",
        }
    ]
    assert audit["result_fact_ids"] == []


async def test_inmemory_audit_roles_grant_audit_exporter() -> None:
    authz = InMemoryAuthz(audit_roles={"user:auditor"})

    assert await authz.check(
        "user:auditor",
        "audit_exporter",
        "export",
    )
    assert not await authz.check(
        "user:bob",
        "audit_exporter",
        "export",
    )


def test_acl_mask_with_audit_role_includes_acl_stamped_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "store.pkl"
    output_path = tmp_path / "audit.graphml"

    public, restricted = _write_store(store_path)

    # Store an untrusted candidate alongside legitimate trusted facts.
    # The privileged graph export must never expose the candidate.
    from ontos.extraction import (
        CandidateProvenance,
        CandidateTriple,
        RejectedCandidate,
    )

    rejected = RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id="candidate-secret-person",
            subject_type="Person",
            subject_canonical_name="Candidate Secret Person",
            predicate="candidate_secret_predicate",
            object_id="candidate-secret-company",
            object_type="Company",
            object_canonical_name="Candidate Secret Company",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id="candidate-secret-source",
            extractor_id="test",
            extractor_version="1",
            ontology_id="starter",
            ontology_version="1",
            confidence_score=0.9,
        ),
        acl_ref="acl:finance",
    )

    async def seed_candidate() -> None:
        store = NetworkxStore(path=store_path)
        await store.add_candidates([rejected])
        await store.close()

    asyncio.run(seed_candidate())

    event_loops: dict[str, object] = {}

    class TrackingAuthz(InMemoryAuthz):
        async def check(
            self,
            subject: str,
            relation: str,
            object_: str,
        ) -> bool:
            event_loops["authz"] = asyncio.get_running_loop()
            return await super().check(subject, relation, object_)

    authz = TrackingAuthz(audit_roles={"user:auditor"})
    monkeypatch.setattr(
        ontos.runtime.server,
        "build_authz",
        lambda _cfg: authz,
    )

    audit_calls: list[dict[str, object]] = []

    class FakeAuditEmitter:
        async def initialize(self) -> None:
            event_loops["audit"] = asyncio.get_running_loop()

        async def emit_async(self, **kwargs: object) -> None:
            audit_calls.append(dict(kwargs))

        async def close(self) -> None:
            return None

    fake_audit = FakeAuditEmitter()
    monkeypatch.setattr(
        "ontos.audit.postgres.PostgresAuditEmitter.from_url",
        lambda *args, **kwargs: fake_audit,
    )

    result = runner.invoke(
        app,
        [
            "export",
            "graphml",
            "--storage-path",
            str(store_path),
            "--output",
            str(output_path),
            "--acting-on-behalf-of",
            "user:auditor",
            "--include-acl-mask",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr

    exported = output_path.read_text(encoding="utf-8")
    assert str(public.id) in exported
    assert str(restricted.id) in exported
    assert "acl:finance" in exported

    # ACL-mask export may reveal trusted fact ACLs, never rejected triples.
    for secret in (
        "candidate-secret-person",
        "candidate-secret-company",
        "Candidate Secret Person",
        "Candidate Secret Company",
        "candidate_secret_predicate",
        "candidate-secret-source",
        "RejectedCandidate",
    ):
        assert secret not in exported, f"Privileged export leaked {secret}"

    assert len(audit_calls) == 1
    assert event_loops["authz"] is event_loops["audit"]

    audit = audit_calls[0]
    assert audit["acting_on_behalf_of"] == "user:auditor"
    assert audit["agent_identity"] == "ontos-cli"
    assert audit["tool_invoked"] == "ontos.export.graphml"

    tool_arguments = audit["tool_arguments"]
    assert isinstance(tool_arguments, dict)
    assert tool_arguments["include_acl_mask"] is True
    assert tool_arguments["fact_count"] == 2
    assert tool_arguments["fact_count_by_acl"] == {
        "acl:finance": 1,
        "public": 1,
    }

    policy_decisions = audit["policy_decisions"]
    assert policy_decisions == [
        {
            "relation": "audit_exporter",
            "object": "export",
            "decision": "allow",
        }
    ]

    result_fact_ids = audit["result_fact_ids"]
    assert isinstance(result_fact_ids, list)
    assert set(result_fact_ids) == {str(public.id), str(restricted.id)}


def test_pushed_neo4j_is_merge_idempotent(tmp_path: Path) -> None:
    """Re-pushing the same snapshot must issue the same MERGE-based writes."""
    from ontos.exporters.cypher import push_neo4j

    store_path = tmp_path / "store.pkl"
    _write_store(store_path)

    store = NetworkxStore(path=store_path)
    scan = asyncio.run(store.scan(include_all_acls=True))
    asyncio.run(store.close())

    calls: list[tuple[str, dict[str, object]]] = []
    databases: list[str] = []

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            tb: object,
        ) -> None:
            return None

        async def run(self, query: str, **params: object) -> None:
            calls.append((query, params))

    class FakeDriver:
        def session(self, *, database: str) -> FakeSession:
            databases.append(database)
            return FakeSession()

    driver = FakeDriver()

    async def _push_twice() -> int:
        await push_neo4j(scan, driver, database="ontos-viz")
        first_push_size = len(calls)

        await push_neo4j(scan, driver, database="ontos-viz")
        return first_push_size

    first_push_size = asyncio.run(_push_twice())

    first_push = calls[:first_push_size]
    second_push = calls[first_push_size:]

    assert first_push
    assert first_push == second_push
    assert databases == ["ontos-viz", "ontos-viz"]

    # Index creation is explicitly idempotent via IF NOT EXISTS.
    index_queries = [
        query for query, _params in first_push if "CREATE INDEX IF NOT EXISTS" in query
    ]
    assert index_queries

    # Every graph mutation must use MERGE. No CREATE-based node/edge
    # writes are allowed, because those would duplicate data on re-push.
    graph_writes = [
        query for query, _params in first_push if "CREATE INDEX IF NOT EXISTS" not in query
    ]

    assert graph_writes
    assert all("MERGE" in query for query in graph_writes)
    assert all("CREATE (" not in query for query in graph_writes)

    # Both node and relationship identities must be MERGE-based.
    assert any("MERGE (n:" in query for query in graph_writes)
    assert any("fact_id: $fact_id" in query and "MERGE" in query for query in graph_writes)
