"""M2a compliance: authorized, audited ontology suggestions."""

from __future__ import annotations

import json

import pytest

from ontos.audit.emitter import AuditEmitter
from ontos.authz import ONTOLOGY_REVIEWER, VIEW, InMemoryAuthz
from ontos.extraction import CandidateProvenance, CandidateTriple, RejectedCandidate
from ontos.ontology.loader import EntityType, Ontology, Pattern, RelationType
from ontos.runtime.candidate_inspection import CandidateInspectionDeniedError
from ontos.runtime.models import ArticleTwelveField
from ontos.runtime.ontology_suggestions import list_ontology_suggestions
from ontos.storage.base import candidate_storage_id
from ontos.storage.networkx_store import NetworkxStore


def _ontology() -> Ontology:
    return Ontology(
        id="ontos.starter",
        version="0.1",
        entity_types=[
            EntityType(label="Person"),
            EntityType(label="Company"),
        ],
        relation_types=[
            RelationType(label="works_at"),
            RelationType(label="acquired"),
        ],
        patterns=[
            Pattern(
                subject_type="Person",
                predicate="works_at",
                object_type="Company",
            ),
            Pattern(
                subject_type="Company",
                predicate="acquired",
                object_type="Company",
            ),
        ],
    )


def _candidate(source: str, acl: str) -> RejectedCandidate:
    return RejectedCandidate(
        raw_triple=CandidateTriple(
            subject_id=f"person-{source}",
            subject_type="Person",
            subject_canonical_name=f"Person {source}",
            predicate="acquired",
            object_id=f"company-{source}",
            object_type="Company",
            object_canonical_name=f"Company {source}",
            llm_confidence=0.9,
        ),
        reason="pattern_not_in_ontology",
        provenance=CandidateProvenance(
            source_id=source,
            extractor_id="scripted-test",
            extractor_version="1",
            ontology_id="ontos.starter",
            ontology_version="0.1",
            confidence_score=0.9,
        ),
        acl_ref=acl,
    )


def _audit() -> AuditEmitter:
    return AuditEmitter(signing_key=b"m2a-compliance-test-key")


def _reviewer(*acls: str) -> InMemoryAuthz:
    authz = InMemoryAuthz()
    authz.grant("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
    for acl in acls:
        authz.grant("user:reviewer", VIEW, acl)
    return authz


async def _suggest(
    store: NetworkxStore,
    authz: InMemoryAuthz,
    audit: AuditEmitter,
):
    return await list_ontology_suggestions(
        candidate_store=store,
        authz=authz,
        audit=audit,
        ontology=_ontology(),
        subject="user:reviewer",
        agent_identity="agent:test",
    )


def _request_decisions(audit: AuditEmitter) -> list[str]:
    decisions = []
    for record in audit._chain:
        if record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_suggestions":
            policy = json.loads(record.article12[ArticleTwelveField.POLICY_DECISIONS])
            decisions.extend(item["result"] for item in policy)
    return decisions


async def test_non_reviewer_denied_and_audited() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("secret", "acl:finance")])
    authz = InMemoryAuthz()
    audit = _audit()

    with pytest.raises(CandidateInspectionDeniedError):
        await _suggest(store, authz, audit)

    assert _request_decisions(audit) == ["request_started", "deny"]
    assert audit.verify_chain()


async def test_viewless_matches_empty_store() -> None:
    populated = NetworkxStore()
    await populated.add_candidates([_candidate("hidden", "acl:finance")])
    empty = NetworkxStore()

    populated_audit = _audit()
    empty_audit = _audit()
    authz = _reviewer()

    populated_result, _ = await _suggest(populated, authz, populated_audit)
    empty_result, _ = await _suggest(empty, authz, empty_audit)

    assert populated_result == empty_result == []
    assert _request_decisions(populated_audit) == ["request_started", "allow"]
    assert populated_audit.verify_chain()


async def test_cross_acl_aggregation_is_forbidden() -> None:
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("finance-1", "acl:finance"),
            _candidate("finance-2", "acl:finance"),
            _candidate("hr-1", "acl:hr"),
        ]
    )
    authz = _reviewer("acl:finance", "acl:hr")
    audit = _audit()

    suggestions, _ = await _suggest(store, authz, audit)

    assert len(suggestions) == 2
    counts = {s.acl_ref: s.support_count for s in suggestions}
    assert counts == {"acl:finance": 2, "acl:hr": 1}
    assert audit.verify_chain()


async def test_pagination_counts_all_authorized_candidates() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate(f"doc-{i:03d}", "acl:finance") for i in range(125)])
    audit = _audit()

    suggestions, _ = await _suggest(store, _reviewer("acl:finance"), audit)

    assert len(suggestions) == 1
    assert suggestions[0].support_count == 125
    assert len(suggestions[0].supporting_candidate_refs) == 125
    assert _request_decisions(audit) == ["request_started", "allow"]
    assert audit.verify_chain()


async def test_suggestions_do_not_enter_trusted_graph() -> None:
    store = NetworkxStore()
    await store.add_candidates([_candidate("untrusted", "acl:finance")])
    audit = _audit()

    suggestions, _ = await _suggest(store, _reviewer("acl:finance"), audit)

    assert len(suggestions) == 1

    snapshot = await store.scan(allowed_acls=["acl:finance"])
    assert snapshot.facts == []
    assert snapshot.entities == []
    assert audit.verify_chain()


async def test_audit_does_not_contain_raw_candidate_ids() -> None:
    store = NetworkxStore()
    candidate = _candidate("secret-document", "acl:finance")
    await store.add_candidates([candidate])
    audit = _audit()

    suggestions, _ = await _suggest(store, _reviewer("acl:finance"), audit)

    assert len(suggestions) == 1
    assert "secret-document" not in suggestions[0].model_dump_json()

    raw_id = candidate_storage_id(candidate)
    for record in audit._chain:
        assert "secret-document" not in str(record.article12)
        # The property HMAC references exist for (#72): no raw storage ids.
        assert raw_id not in str(record.article12)


async def test_storage_failure_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = NetworkxStore()
    audit = _audit()

    async def fail_storage(**kwargs: object) -> None:
        raise RuntimeError("sensitive storage error")

    monkeypatch.setattr(store, "list_candidate_records", fail_storage)

    with pytest.raises(RuntimeError, match="sensitive storage error"):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == ["request_started", "storage_error"]
    assert audit.verify_chain()
    assert "sensitive storage error" not in str(audit._chain[-1].article12)


async def test_candidate_limit_fails_without_partial_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.runtime.ontology_suggestions as service

    monkeypatch.setattr(service, "MAX_CANDIDATES", 1)

    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("first", "acl:finance"),
            _candidate("second", "acl:finance"),
        ]
    )
    audit = _audit()

    with pytest.raises(service.SuggestionLimitExceededError):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == ["request_started", "incomplete_result"]
    assert audit.verify_chain()


async def test_generation_failure_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontos.runtime.ontology_suggestions as service

    store = NetworkxStore()
    await store.add_candidates([_candidate("first", "acl:finance")])
    audit = _audit()

    def fail_generation(**kwargs: object) -> None:
        raise RuntimeError("sensitive generation error")

    monkeypatch.setattr(service, "suggest_missing_patterns", fail_generation)

    with pytest.raises(RuntimeError, match="sensitive generation error"):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == ["request_started", "generation_error"]
    assert audit.verify_chain()
    assert "sensitive generation error" not in str(audit._chain[-1].article12)


async def test_audit_failure_prevents_candidate_storage_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = NetworkxStore()
    audit = _audit()
    storage_called = False

    async def record_storage_access(**kwargs: object) -> None:
        nonlocal storage_called
        storage_called = True
        raise AssertionError("Storage must not be read")

    def fail_audit(**kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(store, "list_candidate_records", record_storage_access)
    monkeypatch.setattr(audit, "emit", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert not storage_called


async def test_view_revocation_during_retrieval_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence must not be disclosed after its VIEW permission is revoked."""
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("finance-1", "acl:finance"),
            _candidate("finance-2", "acl:finance"),
        ]
    )

    authz = _reviewer("acl:finance")
    audit = _audit()

    original_list = store.list_candidate_records

    async def revoke_after_read(**kwargs: object):
        result = await original_list(**kwargs)
        authz.revoke("user:reviewer", VIEW, "acl:finance")
        return result

    monkeypatch.setattr(store, "list_candidate_records", revoke_after_read)

    with pytest.raises(CandidateInspectionDeniedError, match="Ontology suggestions denied"):
        await _suggest(store, authz, audit)

    assert _request_decisions(audit) == [
        "request_started",
        "deny",
    ]
    assert audit.verify_chain()


async def test_authz_backend_failure_is_classified_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ontos.authz.base import AuthzError

    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("finance-secret", "acl:finance"),
        ]
    )

    authz = _reviewer("acl:finance")
    audit = _audit()

    async def fail_authorization(
        subject: str,
        relation: str,
        object_: str,
    ) -> bool:
        raise AuthzError("sensitive authorization backend failure")

    monkeypatch.setattr(authz, "check", fail_authorization)

    with pytest.raises(AuthzError):
        await _suggest(store, authz, audit)

    assert _request_decisions(audit) == [
        "request_started",
        "authorization_error",
    ]

    assert audit.verify_chain()

    for record in audit._chain:
        assert "finance-secret" not in str(record.article12)
        assert "sensitive authorization backend failure" not in str(record.article12)


async def test_public_candidates_are_separate_and_visible_to_reviewer() -> None:
    """Public candidates require reviewer authorization but no private VIEW grant."""
    store = NetworkxStore()
    await store.add_candidates(
        [
            _candidate("public-document", None),
            _candidate("private-document", "acl:finance"),
        ]
    )

    audit = _audit()
    suggestions, _ = await _suggest(store, _reviewer(), audit)

    assert len(suggestions) == 1
    assert suggestions[0].acl_ref is None
    assert suggestions[0].support_count == 1
    assert len(suggestions[0].supporting_candidate_refs) == 1
    assert _request_decisions(audit) == ["request_started", "allow"]
    assert audit.verify_chain()


async def test_final_allow_audit_failure_returns_no_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final request audit must succeed before results are returned."""
    store = NetworkxStore()
    await store.add_candidates([_candidate("finance-1", "acl:finance")])
    audit = _audit()
    original_emit = audit.emit

    def fail_final_audit(**kwargs: object):
        if kwargs.get("tool_invoked") == "ontology_suggestions" and kwargs.get(
            "policy_decisions"
        ) == [{"policy": "ontology_suggestions", "result": "allow"}]:
            raise RuntimeError("final audit unavailable")
        return original_emit(**kwargs)

    monkeypatch.setattr(audit, "emit", fail_final_audit)

    with pytest.raises(RuntimeError, match="final audit unavailable"):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == [
        "request_started",
        "audit_error",
    ]
    assert audit.verify_chain()


async def test_reviewer_role_revocation_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoking ontology_reviewer mid-request prevents disclosure."""
    store = NetworkxStore()
    await store.add_candidates([_candidate("finance-1", "acl:finance")])

    authz = _reviewer("acl:finance")
    audit = _audit()
    original_list = store.list_candidate_records

    async def revoke_after_read(**kwargs: object):
        result = await original_list(**kwargs)
        authz.revoke("user:reviewer", ONTOLOGY_REVIEWER, "ontology")
        return result

    monkeypatch.setattr(store, "list_candidate_records", revoke_after_read)

    with pytest.raises(CandidateInspectionDeniedError, match="Ontology suggestions denied"):
        await _suggest(store, authz, audit)

    assert _request_decisions(audit) == [
        "request_started",
        "deny",
    ]
    assert audit.verify_chain()


async def test_page_audit_failure_is_not_classified_as_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-page inspection audit outage must be identified correctly."""
    from ontos.runtime.candidate_inspection import CandidateInspectionAuditError

    store = NetworkxStore()
    await store.add_candidates([_candidate("finance-1", "acl:finance")])
    audit = _audit()
    original_emit = audit.emit

    def fail_inspection_audit(**kwargs: object):
        if kwargs.get("tool_invoked") == "candidates":
            raise RuntimeError("inspection audit unavailable")
        return original_emit(**kwargs)

    monkeypatch.setattr(audit, "emit", fail_inspection_audit)

    with pytest.raises(CandidateInspectionAuditError):
        await _suggest(store, _reviewer("acl:finance"), audit)

    assert _request_decisions(audit) == [
        "request_started",
        "audit_error",
    ]
    assert audit.verify_chain()


async def test_failed_failure_audit_is_logged_and_original_error_raised() -> None:
    """#85: when the audit sink fails after `request_started`, the trail has
    no outcome record, so the secondary failure must at least be logged."""
    from structlog.testing import capture_logs

    from ontos.runtime.candidate_inspection import CandidateInspectionAuditError

    class SinkDownAfterStart(AuditEmitter):
        def emit(self, **kwargs):  # type: ignore[no-untyped-def]
            decisions = kwargs["policy_decisions"]
            if decisions[0]["result"] != "request_started":
                raise ConnectionError("audit sink down")
            return super().emit(**kwargs)

    store = NetworkxStore()
    await store.add_candidates([_candidate("finance-1", "acl:finance")])
    audit = SinkDownAfterStart(signing_key=b"suggestions-test-key")

    with capture_logs() as logs, pytest.raises(CandidateInspectionAuditError):
        await _suggest(store, _reviewer("acl:finance"), audit)

    events = [
        entry for entry in logs if entry["event"] == "ontology-suggestions-failure-audit-failed"
    ]
    assert events == [
        {
            "event": "ontology-suggestions-failure-audit-failed",
            "error_type": "ConnectionError",
            "original_error_type": "CandidateInspectionAuditError",
            "log_level": "error",
        }
    ]
    assert "audit sink down" not in str(logs)


async def test_view_revoked_during_generation_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No partial suggestions may escape after VIEW revocation."""
    import ontos.runtime.ontology_suggestions as service

    store = NetworkxStore()
    await store.add_candidates([_candidate("revoked-source", "acl:finance")])

    authz = _reviewer("acl:finance")
    audit = _audit()
    original = service.suggest_missing_patterns

    def revoke_during_generation(**kwargs: object):
        suggestions = original(**kwargs)
        assert suggestions
        authz.revoke("user:reviewer", VIEW, "acl:finance")
        return suggestions

    monkeypatch.setattr(service, "suggest_missing_patterns", revoke_during_generation)

    with pytest.raises(
        CandidateInspectionDeniedError,
        match="Ontology suggestions denied",
    ):
        await _suggest(store, authz, audit)

    assert _request_decisions(audit) == [
        "request_started",
        "deny",
    ]
    assert audit.verify_chain()


async def test_m2b_runtime_isolates_acls_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2b reuses inspected, authorized evidence, without graph writes."""
    import ontos.runtime.ontology_suggestions as service
    from ontos.llm.ollama import OllamaBackend

    store = NetworkxStore()
    await store.add_candidates(
        [
            _m2b_candidate("finance", "acl:finance"),
            _m2b_candidate("legal", "acl:legal"),
            _m2b_candidate("hidden", "acl:hidden"),
        ]
    )

    calls: list[dict[str, object]] = []

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        payload = json.loads(user)
        calls.append(payload)
        return {
            "proposals": [
                {
                    "candidate_id": 0,
                    "kind": "map_to_existing",
                    "relation_type": "works_at",
                }
            ]
        }

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    audit = _audit()
    suggestions, _ = await service.list_ontology_suggestions(
        candidate_store=store,
        authz=_reviewer("acl:finance", "acl:legal"),
        audit=audit,
        ontology=_ontology(),
        subject="user:reviewer",
        agent_identity="agent:test",
        backend=OllamaBackend("scripted"),
    )

    assert len(calls) == 2
    assert len(suggestions) == 2
    assert {s.acl_ref for s in suggestions} == {"acl:finance", "acl:legal"}
    assert all(s.kind == "map_to_existing" for s in suggestions)
    assert all(s.observed_predicate == "advises" for s in suggestions)
    assert all(s.mapped_relation_type == "works_at" for s in suggestions)
    assert all(s.support_count == 1 for s in suggestions)

    for call in calls:
        evidence = call["evidence"]
        assert len(evidence) == 1
        assert evidence[0]["predicate"] == "advises"
        assert "hidden" not in str(call)
        assert "acl:" not in str(call)

    snapshot = await store.scan(allowed_acls=["acl:finance", "acl:legal"])
    assert snapshot.facts == []
    assert snapshot.entities == []

    assert _request_decisions(audit) == ["request_started", "allow"]
    assert audit.verify_chain()


async def test_m2b_runtime_refuses_bridge_before_candidate_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge opt-in never bypasses the M2b production trust gate."""
    import ontos.runtime.ontology_suggestions as service
    from ontos.ontology.suggestion_backend import (
        SuggestionBackendRefusedError,
    )

    monkeypatch.setenv("ONTOS_ALLOW_BRIDGE_BACKENDS", "1")

    class Bridge:
        id = "bridge"
        version = "1"
        sovereign = False

        async def structured_suggest(
            self, *, system: str, user: str, schema: dict[str, object]
        ) -> dict[str, object]:
            pytest.fail("Bridge must not receive candidate evidence")

    store = NetworkxStore()
    await store.add_candidates([_candidate("finance", "acl:finance")])

    async def forbidden_read(**kwargs: object) -> None:
        pytest.fail("Candidate storage accessed before backend approval")

    monkeypatch.setattr(store, "list_candidate_records", forbidden_read)

    audit = _audit()
    with pytest.raises(SuggestionBackendRefusedError):
        await service.list_ontology_suggestions(
            candidate_store=store,
            authz=_reviewer("acl:finance"),
            audit=audit,
            ontology=_ontology(),
            subject="user:reviewer",
            agent_identity="agent:test",
            backend=Bridge(),
        )

    assert _request_decisions(audit) == ["request_started", "generation_error"]
    assert audit.verify_chain()


async def test_m2b_audit_counts_invalid_proposals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forged model metadata is rejected and only its count is audited."""
    from ontos.llm.ollama import OllamaBackend

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        return {
            "proposals": [
                {
                    "candidate_id": 0,
                    "kind": "map_to_existing",
                    "relation_type": "works_at",
                    "support_count": 999,
                    "acl_ref": "forged-admin-acl",
                }
            ]
        }

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    store = NetworkxStore()
    await store.add_candidates([_m2b_candidate("finance-secret", "acl:finance")])
    audit = _audit()

    suggestions, final_audit = await list_ontology_suggestions(
        candidate_store=store,
        authz=_reviewer("acl:finance"),
        audit=audit,
        ontology=_ontology(),
        subject="user:reviewer",
        agent_identity="agent:test",
        backend=OllamaBackend("scripted"),
    )

    assert suggestions == []
    assert _request_decisions(audit) == [
        "request_started",
        "allow",
    ]

    arguments = json.loads(final_audit.article12[ArticleTwelveField.TOOL_ARGUMENTS])
    assert arguments["invalid_count"] == 1
    assert arguments["candidate_count"] == 1
    assert arguments["suggestion_count"] == 0

    start = next(
        record
        for record in audit._chain
        if record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_suggestions"
    )
    start_arguments = json.loads(start.article12[ArticleTwelveField.TOOL_ARGUMENTS])
    assert start_arguments["invalid_count"] == 0

    for record in audit._chain:
        serialized = str(record.article12)
        assert "finance-secret" not in serialized
        assert "forged-admin-acl" not in serialized

    assert audit.verify_chain()


async def test_m2b_view_revoked_during_llm_call_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid model result must not escape after source VIEW revocation."""
    from ontos.llm.ollama import OllamaBackend

    store = NetworkxStore()
    await store.add_candidates([_m2b_candidate("revoked-private-source", "acl:finance")])

    authz = _reviewer("acl:finance")
    audit = _audit()
    model_calls = 0

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        nonlocal model_calls
        model_calls += 1

        # Evidence was initially authorized, but the permission changes
        # while the model is processing its request.
        assert len(json.loads(user)["evidence"]) == 1
        authz.revoke("user:reviewer", VIEW, "acl:finance")

        return {
            "proposals": [
                {
                    "candidate_id": 0,
                    "kind": "map_to_existing",
                    "relation_type": "works_at",
                }
            ]
        }

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    with pytest.raises(
        CandidateInspectionDeniedError,
        match="Ontology suggestions denied",
    ):
        await list_ontology_suggestions(
            candidate_store=store,
            authz=authz,
            audit=audit,
            ontology=_ontology(),
            subject="user:reviewer",
            agent_identity="agent:test",
            backend=OllamaBackend("scripted"),
        )

    assert model_calls == 1
    assert _request_decisions(audit) == [
        "request_started",
        "deny",
    ]
    assert audit.verify_chain()

    # Neither the proposal nor its source may leak into audit records.
    for record in audit._chain:
        serialized = str(record.article12)
        assert "revoked-private-source" not in serialized
        assert "mapped_relation_type" not in serialized


async def test_m2b_failure_on_second_acl_aborts_entire_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later model failure must not return earlier ACL suggestions."""
    from ontos.llm.ollama import OllamaBackend

    store = NetworkxStore()
    await store.add_candidates(
        [
            _m2b_candidate("finance-secret", "acl:finance"),
            _m2b_candidate("legal-secret", "acl:legal"),
        ]
    )

    authz = _reviewer("acl:finance", "acl:legal")
    audit = _audit()
    model_calls = 0

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        nonlocal model_calls
        model_calls += 1

        assert len(json.loads(user)["evidence"]) == 1

        if model_calls == 2:
            raise RuntimeError("Scripted model failure")

        return {
            "proposals": [
                {
                    "candidate_id": 0,
                    "kind": "map_to_existing",
                    "relation_type": "works_at",
                }
            ]
        }

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    with pytest.raises(RuntimeError, match="Scripted model failure"):
        await list_ontology_suggestions(
            candidate_store=store,
            authz=authz,
            audit=audit,
            ontology=_ontology(),
            subject="user:reviewer",
            agent_identity="agent:test",
            backend=OllamaBackend("scripted"),
        )

    assert model_calls == 2

    assert _request_decisions(audit) == [
        "request_started",
        "generation_error",
    ]
    assert audit.verify_chain()

    for record in audit._chain:
        serialized = str(record.article12)
        assert "finance-secret" not in serialized
        assert "legal-secret" not in serialized
        assert "Scripted model failure" not in serialized
        assert "mapped_relation_type" not in serialized


async def test_m2b_final_audit_failure_prevents_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid LLM result cannot escape when its final audit fails."""
    from ontos.llm.ollama import OllamaBackend

    store = NetworkxStore()
    await store.add_candidates([_m2b_candidate("finance-secret", "acl:finance")])
    audit = _audit()
    original_emit = audit.emit
    model_calls = 0

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        nonlocal model_calls
        model_calls += 1
        assert len(json.loads(user)["evidence"]) == 1

        return {
            "proposals": [
                {
                    "candidate_id": 0,
                    "kind": "map_to_existing",
                    "relation_type": "works_at",
                }
            ]
        }

    def fail_final_audit(**kwargs: object):
        if kwargs.get("tool_invoked") == "ontology_suggestions" and kwargs.get(
            "policy_decisions"
        ) == [{"policy": "ontology_suggestions", "result": "allow"}]:
            raise RuntimeError("M2b final audit unavailable")
        return original_emit(**kwargs)

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)
    monkeypatch.setattr(audit, "emit", fail_final_audit)

    with pytest.raises(RuntimeError, match="M2b final audit unavailable"):
        await list_ontology_suggestions(
            candidate_store=store,
            authz=_reviewer("acl:finance"),
            audit=audit,
            ontology=_ontology(),
            subject="user:reviewer",
            agent_identity="agent:test",
            backend=OllamaBackend("scripted"),
        )

    assert model_calls == 1
    assert _request_decisions(audit) == [
        "request_started",
        "audit_error",
    ]
    assert audit.verify_chain()

    for record in audit._chain:
        serialized = str(record.article12)
        assert "finance-secret" not in serialized
        assert "M2b final audit unavailable" not in serialized
        assert "mapped_relation_type" not in serialized


async def test_m2b_initial_audit_failure_prevents_evidence_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable initial audit blocks storage reads and model calls."""
    from ontos.llm.ollama import OllamaBackend

    store = NetworkxStore()
    await store.add_candidates([_candidate("private-source", "acl:finance")])

    audit = _audit()
    attempted_decisions: list[str] = []

    def fail_audit(**kwargs: object) -> None:
        decisions = kwargs["policy_decisions"]
        assert isinstance(decisions, list)
        attempted_decisions.append(decisions[0]["result"])
        raise RuntimeError("initial audit unavailable")

    async def forbidden_storage(**kwargs: object) -> None:
        pytest.fail("Candidate storage read despite audit outage")

    async def forbidden_model(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        pytest.fail("LLM invoked despite audit outage")

    monkeypatch.setattr(audit, "emit", fail_audit)
    monkeypatch.setattr(store, "list_candidate_records", forbidden_storage)
    monkeypatch.setattr(OllamaBackend, "structured_suggest", forbidden_model)

    with pytest.raises(RuntimeError, match="initial audit unavailable"):
        await list_ontology_suggestions(
            candidate_store=store,
            authz=_reviewer("acl:finance"),
            audit=audit,
            ontology=_ontology(),
            subject="user:reviewer",
            agent_identity="agent:test",
            backend=OllamaBackend("scripted"),
        )

    assert attempted_decisions[0] == "request_started"

    # The failing sink accepted no audit records.
    assert audit._chain == []

    # No sensitive evidence should appear in attempted audit decisions.
    assert "private-source" not in str(attempted_decisions)


async def test_m2b_audit_identifies_llm_generation_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2a and M2b must have distinct request and outcome audit labels."""
    from ontos.llm.ollama import OllamaBackend

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        return {"proposals": []}

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    store = NetworkxStore()
    await store.add_candidates([_candidate("audit-mode-source", "acl:finance")])

    for backend, mode, query in (
        (None, "m2a", "ontology deterministic suggestions"),
        (
            OllamaBackend("scripted"),
            "m2b",
            "ontology llm-assisted suggestions",
        ),
    ):
        audit = _audit()

        await list_ontology_suggestions(
            candidate_store=store,
            authz=_reviewer("acl:finance"),
            audit=audit,
            ontology=_ontology(),
            subject="user:reviewer",
            agent_identity="agent:test",
            backend=backend,
        )

        assert _request_decisions(audit) == [
            "request_started",
            "allow",
        ]

        records = [
            record
            for record in audit._chain
            if record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_suggestions"
        ]

        assert len(records) == 2

        for record in records:
            serialized = str(record.article12)
            assert f"ontology-suggestions-{mode}-v1" in serialized
            assert query in serialized
            assert "audit-mode-source" not in serialized

        assert audit.verify_chain()


@pytest.mark.parametrize("adapter_name", ["openai", "anthropic"])
async def test_m2b_real_bridge_adapters_refused_before_inspection(
    adapter_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actual bridge adapters remain forbidden even with production opt-in."""
    import ontos.runtime.ontology_suggestions as service
    from ontos.llm.anthropic import AnthropicBackend
    from ontos.llm.openai import OpenAIBackend
    from ontos.ontology.suggestion_backend import (
        SuggestionBackendRefusedError,
    )

    monkeypatch.setenv("ONTOS_ENV", "prod")
    monkeypatch.setenv("ONTOS_ALLOW_BRIDGE_BACKENDS", "1")

    if adapter_name == "openai":
        backend = OpenAIBackend(model="gpt-4o-mini")
    else:
        backend = AnthropicBackend(model="claude-sonnet-4")

    assert backend.sovereign is False

    async def forbidden_inspection(
        **kwargs: object,
    ) -> None:
        pytest.fail("Candidate inspection occurred before bridge refusal")

    monkeypatch.setattr(
        service,
        "inspect_candidates",
        forbidden_inspection,
    )

    audit = _audit()
    store = NetworkxStore()

    with pytest.raises(SuggestionBackendRefusedError):
        await service.list_ontology_suggestions(
            candidate_store=store,
            authz=_reviewer("acl:finance"),
            audit=audit,
            ontology=_ontology(),
            subject="user:reviewer",
            agent_identity="agent:test",
            backend=backend,
        )

    assert _request_decisions(audit) == [
        "request_started",
        "generation_error",
    ]
    assert audit.verify_chain()


async def test_m2b_audit_records_approved_generator_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an approved model identity enters outcome audit metadata."""
    from ontos.llm.ollama import OllamaBackend

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        return {"proposals": []}

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    store = NetworkxStore()
    await store.add_candidates([_candidate("model-audit-source", "acl:finance")])

    audit = _audit()
    await list_ontology_suggestions(
        candidate_store=store,
        authz=_reviewer("acl:finance"),
        audit=audit,
        ontology=_ontology(),
        subject="user:reviewer",
        agent_identity="agent:test",
        backend=OllamaBackend("scripted-audit-v1"),
    )

    records = [
        record
        for record in audit._chain
        if record.article12[ArticleTwelveField.TOOL_INVOKED] == "ontology_suggestions"
    ]
    assert len(records) == 2

    initial = str(records[0].article12)
    outcome = str(records[1].article12)

    assert "generator_id" not in initial
    assert "generator_version" not in initial
    assert "generator_id" in outcome
    assert "ollama:scripted-audit-v1" in outcome
    assert "generator_version" in outcome
    assert "scripted-audit-v1" in outcome
    assert "model-audit-source" not in outcome
    assert audit.verify_chain()


async def test_m2b_generation_failure_audits_approved_generator_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing approved model is identifiable without leaking evidence."""
    from ontos.llm.ollama import OllamaBackend

    async def failing(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        raise RuntimeError("Scripted generation failure")

    monkeypatch.setattr(OllamaBackend, "structured_suggest", failing)

    store = NetworkxStore()
    await store.add_candidates([_m2b_candidate("failure-private-source", "acl:finance")])

    audit = _audit()

    with pytest.raises(RuntimeError, match="Scripted generation failure"):
        await list_ontology_suggestions(
            candidate_store=store,
            authz=_reviewer("acl:finance"),
            audit=audit,
            ontology=_ontology(),
            subject="user:reviewer",
            agent_identity="agent:test",
            backend=OllamaBackend("failing-model-v2"),
        )

    assert _request_decisions(audit) == [
        "request_started",
        "generation_error",
    ]

    outcome = str(audit._chain[-1].article12)
    assert "ollama:failing-model-v2" in outcome
    assert "generator_version" in outcome
    assert "failure-private-source" not in outcome
    assert "Scripted generation failure" not in outcome
    assert audit.verify_chain()


async def test_m2b_combines_deterministic_and_llm_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known relations use M2a; only unknown relations reach Ollama."""
    from ontos.llm.ollama import OllamaBackend

    deterministic = _candidate("known", "acl:finance")
    novel = _candidate("novel", "acl:finance")
    novel = novel.model_copy(
        update={"raw_triple": novel.raw_triple.model_copy(update={"predicate": "advises"})}
    )

    store = NetworkxStore()
    await store.add_candidates([deterministic, novel])

    calls: list[list[dict[str, object]]] = []

    async def scripted(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        evidence = json.loads(user)["evidence"]
        calls.append(evidence)
        return {
            "proposals": [
                {
                    "candidate_id": 0,
                    "kind": "new_relation_type",
                    "relation_type": "advises",
                }
            ]
        }

    monkeypatch.setattr(OllamaBackend, "structured_suggest", scripted)

    audit = _audit()
    suggestions, _ = await list_ontology_suggestions(
        candidate_store=store,
        authz=_reviewer("acl:finance"),
        audit=audit,
        ontology=_ontology(),
        subject="user:reviewer",
        agent_identity="agent:test",
        backend=OllamaBackend("scripted"),
    )

    assert len(calls) == 1
    assert len(calls[0]) == 1
    assert calls[0][0]["predicate"] == "advises"
    assert "acquired" not in str(calls)

    assert {
        (suggestion.kind, suggestion.pattern.predicate)
        for suggestion in suggestions
        if suggestion.pattern is not None
    } == {
        ("add_pattern", "acquired"),
        ("new_relation_type", "advises"),
    }

    assert _request_decisions(audit) == [
        "request_started",
        "allow",
    ]
    assert audit.verify_chain()


async def test_m2b_skips_model_when_m2a_handles_all_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deterministic-only batch must never invoke the LLM."""
    from ontos.llm.ollama import OllamaBackend

    store = NetworkxStore()
    await store.add_candidates([_candidate("known-only", "acl:finance")])

    async def forbidden_model(
        self: OllamaBackend,
        system: str,
        user: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        pytest.fail("M2a candidate unexpectedly sent to Ollama")

    monkeypatch.setattr(
        OllamaBackend,
        "structured_suggest",
        forbidden_model,
    )

    audit = _audit()
    suggestions, _ = await list_ontology_suggestions(
        candidate_store=store,
        authz=_reviewer("acl:finance"),
        audit=audit,
        ontology=_ontology(),
        subject="user:reviewer",
        agent_identity="agent:test",
        backend=OllamaBackend("scripted"),
    )

    assert len(suggestions) == 1
    assert suggestions[0].kind == "add_pattern"
    assert audit.verify_chain()


def _m2b_candidate(source: str, acl: str | None):
    """An unknown relation that must reach the M2b model."""
    candidate = _candidate(source, acl)
    return candidate.model_copy(
        update={"raw_triple": candidate.raw_triple.model_copy(update={"predicate": "advises"})}
    )
