"""Gate-blocking: exports never reveal entities known only via forbidden facts (#100).

`add_fact` creates a bare endpoint node (no type, no ACL) for each id it
references. `scan()` treated every ACL-less node as public, so an export
dropped a restricted fact but still listed its endpoint, which proves
the forbidden fact exists. A bare node may appear only as an endpoint of
a fact (or merge record) the scan actually returns.

The `check_*` bodies are shared with the Neo4j integration tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ontos.cli.main import app
from ontos.runtime.models import Confidence, Entity, Fact, Provenance
from ontos.storage.base import GraphStore
from ontos.storage.networkx_store import NetworkxStore

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})

SECRET = "target:secret-acquisition"


def _fact(subject: str, object_: str, acl_ref: str | None) -> Fact:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    return Fact(
        subject_id=subject,
        predicate="acquired",
        object_id=object_,
        provenance=Provenance(
            source_id="doc:test",
            extractor_id="extractor:test",
            extractor_version="1",
            confidence=Confidence.EXTRACTED,
            confidence_score=1.0,
        ),
        t_valid=when,
        ingested_at=when,
        acl_ref=acl_ref,
    )


async def _seed(store: GraphStore) -> tuple[Fact, Fact]:
    public = _fact("company:acme", "company:widget", None)
    restricted = _fact("company:acme", SECRET, "acl:finance")
    await store.upsert_entity(
        Entity(
            id="company:lonely",
            type="Company",
            canonical_name="Lonely Co",
            provenance=public.provenance,
        )
    )
    await store.add_fact(public)
    await store.add_fact(restricted)
    return public, restricted


async def check_scan_hides_raw_endpoints_of_forbidden_facts(store: GraphStore) -> None:
    public, restricted = await _seed(store)

    scan = await store.scan()

    entity_ids = {entity.id for entity in scan.entities}
    assert [fact.id for fact in scan.facts] == [public.id]
    assert SECRET not in entity_ids
    # Raw endpoints of visible facts and typed public entities stay.
    assert {"company:acme", "company:widget", "company:lonely"} <= entity_ids


async def check_scan_shows_raw_endpoints_to_permitted_callers(store: GraphStore) -> None:
    await _seed(store)

    scan = await store.scan(allowed_acls=["acl:finance"])

    assert SECRET in {entity.id for entity in scan.entities}


async def check_privileged_scan_keeps_every_node(store: GraphStore) -> None:
    await _seed(store)

    scan = await store.scan(include_all_acls=True)

    assert SECRET in {entity.id for entity in scan.entities}


CHECKS = [
    check_scan_hides_raw_endpoints_of_forbidden_facts,
    check_scan_shows_raw_endpoints_to_permitted_callers,
    check_privileged_scan_keeps_every_node,
]


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_networkx_store(check) -> None:  # type: ignore[no-untyped-def]
    await check(NetworkxStore())


@pytest.mark.parametrize("export_format", ["graphml", "json-ld", "neo4j"])
def test_no_export_format_names_the_forbidden_endpoint(
    export_format: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}")
    monkeypatch.setenv("ONTOS_AUDIT_SIGNING_KEY", "scan-leak-test-key")
    store_path = tmp_path / "store.pkl"

    async def seed() -> None:
        store = NetworkxStore(path=store_path)
        await _seed(store)
        await store.close()

    asyncio.run(seed())
    output = tmp_path / f"export.{export_format}"

    result = runner.invoke(
        app,
        [
            "export",
            export_format,
            "--storage-path",
            str(store_path),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    exported = output.read_text(encoding="utf-8")
    assert "company:widget" in exported
    assert SECRET not in exported
