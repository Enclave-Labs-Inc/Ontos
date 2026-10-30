"""CLI contract tests for ontology suggestions (issue #66, M2a)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ontos.cli import app

runner = CliRunner()
STARTER = Path(__file__).resolve().parents[2] / "docs" / "ontology" / "examples" / "starter.yaml"


def test_suggestions_command_registered() -> None:
    result = runner.invoke(app, ["ontology", "--help"])

    assert result.exit_code == 0
    assert "suggestions" in result.stdout


def test_suggestions_list_help() -> None:
    # Introspect the parsed command rather than rendered help: under
    # GITHUB_ACTIONS rich ignores COLUMNS and truncates long option names.
    import typer

    root = typer.main.get_command(app)
    ontology = root.commands["ontology"]  # type: ignore[attr-defined]
    command = ontology.commands["suggestions"].commands["list"]
    options = {opt for param in command.params for opt in param.opts}

    assert {"--acting-on-behalf-of", "--ontology", "--storage-path"} <= options
    assert runner.invoke(app, ["ontology", "suggestions", "list", "--help"]).exit_code == 0


def test_suggestions_requires_identity_and_ontology() -> None:
    result = runner.invoke(app, ["ontology", "suggestions", "list"])

    assert result.exit_code != 0


def test_suggestions_requires_persistent_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", "")

    result = runner.invoke(
        app,
        [
            "ontology",
            "suggestions",
            "list",
            "--acting-on-behalf-of",
            "user:reviewer",
            "--ontology",
            str(STARTER),
            "--storage-path",
            str(tmp_path / "store.pkl"),
        ],
    )

    assert result.exit_code != 0
    assert "Persistent audit database is required" in result.output


def test_suggestions_requires_audit_signing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ONTOS_AUDIT_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}",
    )
    monkeypatch.delenv("ONTOS_AUDIT_SIGNING_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "ontology",
            "suggestions",
            "list",
            "--acting-on-behalf-of",
            "user:reviewer",
            "--ontology",
            str(STARTER),
            "--storage-path",
            str(tmp_path / "store.pkl"),
        ],
    )

    assert result.exit_code != 0
    assert "Audit signing key is required" in result.output


def test_suggestions_rejects_invalid_ontology(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "ontology",
            "suggestions",
            "list",
            "--acting-on-behalf-of",
            "user:reviewer",
            "--ontology",
            str(tmp_path / "nonexistent.yaml"),
        ],
    )

    assert result.exit_code != 0
    assert "Unable to load ontology" in result.output


def _seed_suggestion_candidates(
    path: Path,
    *,
    acl: str = "acl:finance",
) -> tuple[str, str, str]:
    """Persist two candidates for an existing relation but missing pattern."""
    import asyncio

    from ontos.extraction import (
        CandidateProvenance,
        CandidateTriple,
        RejectedCandidate,
    )
    from ontos.ontology import load_ontology
    from ontos.storage.networkx_store import NetworkxStore

    ontology = load_ontology(STARTER)

    missing = next(
        (
            (subject.label, relation.label, object_.label)
            for subject in ontology.entity_types
            for relation in ontology.relation_types
            for object_ in ontology.entity_types
            if not ontology.is_valid_pattern(subject.label, relation.label, object_.label)
        ),
        None,
    )
    assert missing is not None

    subject_type, predicate, object_type = missing

    def candidate(source: str) -> RejectedCandidate:
        return RejectedCandidate(
            raw_triple=CandidateTriple(
                subject_id=f"subject-{source}",
                subject_type=subject_type,
                subject_canonical_name=f"Subject {source}",
                predicate=predicate,
                object_id=f"object-{source}",
                object_type=object_type,
                object_canonical_name=f"Object {source}",
                llm_confidence=0.9,
            ),
            reason="pattern_not_in_ontology",
            provenance=CandidateProvenance(
                source_id=source,
                extractor_id="scripted-test",
                extractor_version="1",
                ontology_id=ontology.id,
                ontology_version=ontology.version,
                confidence_score=0.9,
            ),
            acl_ref=acl,
        )

    async def seed() -> None:
        store = NetworkxStore(path=path)
        try:
            await store.add_candidates([candidate("finance-one"), candidate("finance-two")])
        finally:
            await store.close()

    asyncio.run(seed())
    return missing


def _suggestions_cli_args(path: Path, subject: str) -> list[str]:
    return [
        "ontology",
        "suggestions",
        "list",
        "--acting-on-behalf-of",
        subject,
        "--ontology",
        str(STARTER),
        "--storage-path",
        str(path),
    ]


def test_suggestions_cli_authorized_json_and_view_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from ontos.authz import ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
    from ontos.ontology import load_ontology

    path = tmp_path / "candidates.pkl"
    expected_pattern = _seed_suggestion_candidates(path)
    ontology = load_ontology(STARTER)

    monkeypatch.setenv(
        "ONTOS_AUDIT_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}",
    )
    monkeypatch.setenv(
        "ONTOS_AUDIT_SIGNING_KEY",
        "m2a-cli-test-signing-key",
    )

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:finance")

    monkeypatch.setattr(
        "ontos.runtime.server.build_authz",
        lambda cfg: authz,
    )

    args = _suggestions_cli_args(path, "user:reviewer")
    allowed = runner.invoke(app, args)

    assert allowed.exit_code == 0, allowed.output
    payload = json.loads(allowed.stdout)
    assert payload["ontology_id"] == ontology.id
    assert payload["ontology_version"] == ontology.version

    assert len(payload["suggestions"]) == 1
    suggestion = payload["suggestions"][0]
    assert suggestion["kind"] == "add_pattern"
    assert suggestion["pattern"] == dict(
        zip(
            ("subject_type", "predicate", "object_type"),
            expected_pattern,
            strict=True,
        )
    )
    assert suggestion["acl_ref"] == "acl:finance"
    assert suggestion["support_count"] == 2
    assert len(suggestion["supporting_candidate_refs"]) == 2

    for ref in suggestion["supporting_candidate_refs"]:
        assert len(ref) == 64
        assert all(ch in "0123456789abcdef" for ch in ref)

    assert "finance-one" not in allowed.stdout
    assert "finance-two" not in allowed.stdout

    authz.revoke("user:reviewer", VIEW, "acl:finance")

    no_view = runner.invoke(app, args)
    assert no_view.exit_code == 0, no_view.output
    assert json.loads(no_view.stdout)["suggestions"] == []


def test_suggestions_cli_non_reviewer_denial_persistently_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import json

    from ontos.audit.postgres import PostgresAuditEmitter
    from ontos.authz import VIEW, InMemoryAuthz
    from ontos.runtime.models import ArticleTwelveField

    path = tmp_path / "candidates.pkl"
    _seed_suggestion_candidates(path)

    audit_url = f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}"
    monkeypatch.setenv("ONTOS_AUDIT_DB_URL", audit_url)
    monkeypatch.setenv(
        "ONTOS_AUDIT_SIGNING_KEY",
        "m2a-cli-test-signing-key",
    )

    authz = InMemoryAuthz()
    authz.grant("user:employee", VIEW, "acl:finance")
    monkeypatch.setattr(
        "ontos.runtime.server.build_authz",
        lambda cfg: authz,
    )

    result = runner.invoke(
        app,
        _suggestions_cli_args(path, "user:employee"),
    )

    assert result.exit_code == 1
    assert "Ontology suggestions denied" in result.output
    assert "finance-one" not in result.output
    assert "finance-two" not in result.output

    async def verify() -> None:
        audit = PostgresAuditEmitter.from_url(audit_url)
        try:
            assert await audit.verify_chain_async()
            assert await audit.count_async() == 3

            from sqlalchemy import select

            async with audit._engine.connect() as conn:
                rows = (await conn.execute(select(audit._table))).mappings().all()

            decisions = [
                json.loads(record["article12"][ArticleTwelveField.POLICY_DECISIONS.value])
                for record in rows
            ]
            flattened = [entry["result"] for policy in decisions for entry in policy]
            assert "request_started" in flattened
            assert "deny" in flattened
        finally:
            await audit.close()

    asyncio.run(verify())


def test_m2b_cli_invokes_model_only_with_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI selects M2b; VIEW-less evidence never reaches the model."""
    import json

    from ontos.authz import ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
    from ontos.llm.ollama import OllamaBackend

    path = tmp_path / "m2b-candidates.pkl"
    expected_pattern = _seed_suggestion_candidates(path)

    monkeypatch.setenv(
        "ONTOS_AUDIT_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'm2b-audit.sqlite'}",
    )
    monkeypatch.setenv(
        "ONTOS_AUDIT_SIGNING_KEY",
        "m2b-cli-scripted-signing-key",
    )

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:finance")

    monkeypatch.setattr(
        "ontos.runtime.server.build_authz",
        lambda cfg: authz,
    )

    calls: list[tuple[str, dict[str, object]]] = []

    async def scripted(
        self: OllamaBackend,
        *,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        payload = json.loads(user)
        calls.append((self.id, payload))

        assert schema["type"] == "object"
        assert "SECRET" not in user

        return {"proposals": []}

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    args = _suggestions_cli_args(path, "user:reviewer") + ["--llm-model", "scripted-m2b"]

    allowed = runner.invoke(app, args)
    assert allowed.exit_code == 0, allowed.output

    payload = json.loads(allowed.stdout)
    assert payload["suggestions"] == []

    # Two candidates with the same pattern and ACL produce one request.
    assert len(calls) == 1
    assert calls[0][0] == "ollama:scripted-m2b"

    evidence = calls[0][1]["evidence"]
    assert len(evidence) == 1
    assert (
        evidence[0]["subject_type"],
        evidence[0]["predicate"],
        evidence[0]["object_type"],
    ) == expected_pattern

    assert "finance-one" not in str(calls)
    assert "finance-two" not in str(calls)
    assert "acl:finance" not in str(calls)
    assert "finance-one" not in allowed.stdout
    assert "finance-two" not in allowed.stdout

    # Reviewer role remains, but source VIEW is revoked.
    authz.revoke("user:reviewer", VIEW, "acl:finance")

    no_view = runner.invoke(app, args)
    assert no_view.exit_code == 0, no_view.output
    assert json.loads(no_view.stdout)["suggestions"] == []

    # No additional model request is permitted.
    assert len(calls) == 1


def test_m2b_cli_refuses_remote_endpoint_before_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untrusted Ollama configuration fails closed through the CLI."""
    from ontos.authz import ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
    from ontos.llm.ollama import OllamaBackend

    storage_path = tmp_path / "remote-test.pkl"
    _seed_suggestion_candidates(storage_path)

    monkeypatch.setenv(
        "ONTOS_AUDIT_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'remote-audit.sqlite'}",
    )
    monkeypatch.setenv(
        "ONTOS_AUDIT_SIGNING_KEY",
        "remote-endpoint-scripted-signing-key",
    )

    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    authz.grant("user:reviewer", VIEW, "acl:finance")

    monkeypatch.setattr(
        "ontos.runtime.server.build_authz",
        lambda cfg: authz,
    )

    remote_backend = OllamaBackend(
        "scripted-m2b",
        base_url="http://10.0.0.5:11434",
    )

    async def forbidden_inspection(*args: object, **kwargs: object) -> None:
        pytest.fail("Candidate inspection started for remote backend")

    async def forbidden_model(
        *,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        pytest.fail("Remote backend received evidence")

    monkeypatch.setattr(
        "ontos.runtime.ontology_suggestions.inspect_candidates",
        forbidden_inspection,
    )
    monkeypatch.setattr(remote_backend, "structured_suggest", forbidden_model)

    # Replace only the CLI's backend factory. The production trust
    # module still holds the real OllamaBackend class and checks the
    # actual returned instance.
    monkeypatch.setattr(
        "ontos.llm.ollama.OllamaBackend",
        lambda model: remote_backend,
    )

    args = _suggestions_cli_args(storage_path, "user:reviewer") + ["--llm-model", "scripted-m2b"]

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "Ontology suggestions failed." in result.output
    assert "10.0.0.5" not in result.output
    assert "remote-test.pkl" not in result.output
    assert "Traceback" not in result.output
