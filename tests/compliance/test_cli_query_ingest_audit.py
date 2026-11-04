"""Gate-blocking: `ontos query` and `ontos ingest` are audited (#94).

Both commands touched the graph with no Article-12 record, and `ontos
query` could not run as a subject, so it only ever saw public facts.
Each invocation must now leave exactly one record, written before any
result is printed, and must not run at all without a working sink.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import ontos.extraction
import ontos.llm
import ontos.planner
import ontos.runtime.server
from ontos import __version__
from ontos.audit.postgres import PostgresAuditEmitter
from ontos.authz import VIEW, InMemoryAuthz
from ontos.cli import app
from ontos.extraction.base import ExtractionResult
from ontos.planner import Plan, PlannerError, SeedByEntity, TraversalStep
from ontos.runtime.models import ArticleTwelveField, AuditRecord
from ontos.storage.networkx_store import NetworkxStore
from tests.compliance._mcp_harness import make_fact

STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"
_KEY = "cli-audit-test-key"


@pytest.fixture
def audit_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}"
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", url)
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", _KEY)
    monkeypatch.delenv("ONTOS_AUTHZ_BACKEND", raising=False)
    monkeypatch.setattr(ontos.llm, "OllamaBackend", _StubLLM)
    return url


def _records(url: str) -> list[AuditRecord]:
    async def read() -> list[AuditRecord]:
        audit = PostgresAuditEmitter.from_url(url, signing_key_env="ONTOS_AUDIT_SIGNING_KEY")
        try:
            await audit.initialize()
            assert await audit.verify_chain_async()
            from sqlalchemy import select

            from ontos.audit.postgres import _audit_table, _record_from_row

            async with audit._engine.connect() as conn:
                rows = await conn.execute(select(_audit_table).order_by(_audit_table.c.ordinal))
                return [_record_from_row(dict(row)) for row in rows.mappings()]
        finally:
            await audit.close()

    return asyncio.run(read())


def _field(record: AuditRecord, field: ArticleTwelveField) -> Any:
    raw = record.article12[field]
    return json.loads(raw) if field in _JSON_FIELDS else raw


_JSON_FIELDS = {
    ArticleTwelveField.TOOL_ARGUMENTS,
    ArticleTwelveField.RESULT_FACT_IDS,
    ArticleTwelveField.MODEL_VERSIONS,
    ArticleTwelveField.POLICY_DECISIONS,
}


class _StubLLM:
    id = "stub-llm"

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class _SeedAcme:
    id = "stub-planner"

    def __init__(self, llm: object, ontology: object) -> None:
        pass

    async def plan(self, question: str, ontology: object) -> Plan:
        return Plan.build(
            seed=SeedByEntity(entity_id="Acme"),
            steps=[TraversalStep(depth=1, direction="out")],
            ontology=ontology,  # type: ignore[arg-type]
        )


class _FailingPlanner(_SeedAcme):
    async def plan(self, question: str, ontology: object) -> Plan:
        raise PlannerError(question, "model said no")


def _seed_store(path: Path) -> tuple[str, str]:
    public = make_fact("Acme", "acl:placeholder").model_copy(update={"acl_ref": None})
    finance = make_fact("Acme", "acl:finance").model_copy(update={"object_id": "Ledger Co"})

    async def seed() -> None:
        store = NetworkxStore(path=path)
        await store.add_fact(public)
        await store.add_fact(finance)
        await store.close()

    asyncio.run(seed())
    return str(public.id), str(finance.id)


def _query(store: Path, *extra: str) -> Any:
    return CliRunner().invoke(
        app,
        ["query", "who?", "--ontology", str(STARTER), "--storage-path", str(store), *extra],
    )


def test_query_writes_one_record_matching_its_hits(
    audit_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_id, _ = _seed_store(tmp_path / "g.pkl")
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _SeedAcme)

    result = _query(tmp_path / "g.pkl")

    assert result.exit_code == 0, result.stdout + result.stderr
    hits = [hit["fact"]["id"] for hit in json.loads(result.stdout)["hits"]]
    assert hits == [public_id]
    (record,) = _records(audit_url)
    assert _field(record, ArticleTwelveField.TOOL_INVOKED) == "ontos.query"
    assert _field(record, ArticleTwelveField.QUERY_TEXT) == "who?"
    assert _field(record, ArticleTwelveField.RESULT_FACT_IDS) == [public_id]
    assert _field(record, ArticleTwelveField.MODEL_VERSIONS)["runtime"] == __version__
    assert _field(record, ArticleTwelveField.POLICY_DECISIONS)[-1] == {
        "policy": "request",
        "result": "completed",
    }


def test_query_as_a_subject_sees_only_what_it_may_view(
    audit_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_id, finance_id = _seed_store(tmp_path / "g.pkl")
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _SeedAcme)
    authz = InMemoryAuthz()
    authz.grant("user:fin", VIEW, "acl:finance")
    monkeypatch.setattr(ontos.runtime.server, "build_authz", lambda cfg: authz)

    granted = _query(tmp_path / "g.pkl", "--acting-on-behalf-of", "user:fin")
    other = _query(tmp_path / "g.pkl", "--acting-on-behalf-of", "user:other")

    assert granted.exit_code == 0, granted.stdout + granted.stderr
    assert other.exit_code == 0, other.stdout + other.stderr
    assert {h["fact"]["id"] for h in json.loads(granted.stdout)["hits"]} == {
        public_id,
        finance_id,
    }
    assert [h["fact"]["id"] for h in json.loads(other.stdout)["hits"]] == [public_id]
    assert finance_id not in other.stdout

    first, second = _records(audit_url)
    assert _field(first, ArticleTwelveField.ACTING_ON_BEHALF_OF) == "user:fin"
    assert _field(first, ArticleTwelveField.TOOL_ARGUMENTS)["identity_assurance"] == (
        "operator_asserted"
    )
    assert _field(second, ArticleTwelveField.RESULT_FACT_IDS) == [public_id]


def test_query_failure_is_audited(
    audit_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_store(tmp_path / "g.pkl")
    monkeypatch.setattr(ontos.planner, "LlmPlanner", _FailingPlanner)

    result = _query(tmp_path / "g.pkl")

    assert result.exit_code != 0
    (record,) = _records(audit_url)
    assert _field(record, ArticleTwelveField.RESULT_FACT_IDS) == []
    assert _field(record, ArticleTwelveField.POLICY_DECISIONS)[-1]["result"] == "planner_error"


def test_query_does_not_run_without_an_audit_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_store(tmp_path / "g.pkl")
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", "postgresql+psycopg://sync/driver")
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", _KEY)
    monkeypatch.setattr(ontos.llm, "OllamaBackend", _StubLLM)
    planned: list[str] = []

    class _Recording(_SeedAcme):
        async def plan(self, question: str, ontology: object) -> Plan:
            planned.append(question)
            return await super().plan(question, ontology)

    monkeypatch.setattr(ontos.planner, "LlmPlanner", _Recording)

    result = _query(tmp_path / "g.pkl")

    assert result.exit_code == 1
    assert "Audit store unavailable" in result.stderr
    assert result.stdout == ""
    assert planned == []


class _OneFactExtractor:
    id = "stub-extractor"

    def __init__(self, llm: object, ontology: object) -> None:
        pass

    async def extract(self, input: Any) -> ExtractionResult:
        fact = make_fact("Acme", "acl:placeholder").model_copy(
            update={"acl_ref": None, "id": _OneFactExtractor.fact_id}
        )
        return ExtractionResult(facts=[fact], warnings=["SECRET-DOC-TEXT quoted in a warning"])

    fact_id = make_fact("x", "y").id


def _ingest(tmp_path: Path) -> Any:
    source = tmp_path / "corpus"
    source.mkdir(exist_ok=True)
    (source / "doc.txt").write_text("Acme acquired Widget Inc.")
    return CliRunner().invoke(
        app,
        [
            "ingest",
            "--source-dir",
            str(source),
            "--ontology",
            str(STARTER),
            "--storage-path",
            str(tmp_path / "g.pkl"),
        ],
    )


def test_ingest_writes_one_record_with_the_facts_it_wrote(
    audit_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ontos.extraction, "LlmExtractor", _OneFactExtractor)

    result = _ingest(tmp_path)

    assert result.exit_code == 0, result.stdout + result.stderr
    (record,) = _records(audit_url)
    assert _field(record, ArticleTwelveField.TOOL_INVOKED) == "ontos.ingest"
    assert _field(record, ArticleTwelveField.RESULT_FACT_IDS) == [str(_OneFactExtractor.fact_id)]
    arguments = _field(record, ArticleTwelveField.TOOL_ARGUMENTS)
    assert arguments["facts_written"] == 1
    assert arguments["documents_seen"] == 1
    assert _field(record, ArticleTwelveField.MODEL_VERSIONS)["extractor"] == "stub-extractor"
    assert "SECRET-DOC-TEXT" not in json.dumps(record.article12)


def test_ingest_failure_is_audited(
    audit_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Broken(_OneFactExtractor):
        async def extract(self, input: Any) -> ExtractionResult:
            raise RuntimeError("extractor exploded")

    monkeypatch.setattr(ontos.extraction, "LlmExtractor", _Broken)

    result = _ingest(tmp_path)

    assert result.exit_code != 0
    (record,) = _records(audit_url)
    assert _field(record, ArticleTwelveField.RESULT_FACT_IDS) == []
    assert _field(record, ArticleTwelveField.POLICY_DECISIONS)[-1]["result"] == "error"


def test_query_rejects_unverified_migration_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid migration history must block planning and storage access."""
    from uuid import uuid4

    from ontos.migration.loading import MigrationConfigurationError
    from ontos.migration.records import MigrationRecord
    from ontos.ontology import load_ontology

    active = load_ontology(STARTER)
    previous = "0.0" if active.version != "0.0" else "0.1"

    migrations = tmp_path / "migrations"
    history = tmp_path / "history"
    migrations.mkdir()
    history.mkdir()

    # This record has a structurally valid migration edge, but there
    # are no historical ontology files to substantiate it.
    record = MigrationRecord.model_validate(
        {
            "schema_version": 1,
            "ontology_id": active.id,
            "from_version": previous,
            "to_version": active.version,
            "migration_kind": "identity",
            "approval_query_id": str(uuid4()),
            "approved_changes": [
                {
                    "action": "add_pattern",
                    "subject_type": "Person",
                    "predicate": "works_at",
                    "object_type": "Company",
                }
            ],
        }
    )
    (migrations / "migration.yaml").write_text(record.model_dump_json())

    monkeypatch.setenv("ONTOS_MIGRATIONS_DIR", str(migrations))
    monkeypatch.setenv("ONTOS_ONTOLOGY_HISTORY_DIR", str(history))
    monkeypatch.setattr(ontos.llm, "OllamaBackend", _StubLLM)

    planned: list[str] = []

    class RecordingPlanner(_SeedAcme):
        async def plan(self, question: str, ontology: object) -> Plan:
            planned.append(question)
            return await super().plan(question, ontology)

    monkeypatch.setattr(ontos.planner, "LlmPlanner", RecordingPlanner)

    opened_store: list[bool] = []

    def unexpected_store(config: object) -> None:
        opened_store.append(True)
        raise AssertionError("graph store opened before migration verification")

    monkeypatch.setattr(
        ontos.runtime.server,
        "build_store",
        unexpected_store,
    )

    result = _query(tmp_path / "unused.pkl")

    assert result.exit_code != 0
    assert isinstance(result.exception, MigrationConfigurationError)
    assert "history verification failed" in str(result.exception)
    assert planned == []
    assert opened_store == []
    assert '"hits"' not in result.stdout
