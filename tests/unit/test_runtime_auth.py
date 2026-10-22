"""#69 — unit tests for MCP caller authentication helpers."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from ontos.runtime.auth import (
    AuthConfigError,
    CallerRejectedError,
    UnauthenticatedBindError,
    build_token_verifier,
    enforce_bind_policy,
    is_loopback_host,
    parse_auth_mode,
    resolve_caller,
)
from ontos.runtime.config import RuntimeConfig

ISSUER = "https://idp.customer.internal"
AUDIENCE = "ontos"


def _cfg(**overrides: object) -> RuntimeConfig:
    return dataclasses.replace(RuntimeConfig.from_env(), **overrides)  # type: ignore[arg-type]


def _token(claims: dict[str, object], client_id: str = "client:agent") -> AccessToken:
    return AccessToken(token="t", client_id=client_id, scopes=[], claims=claims)


@pytest.fixture
def public_key_file(tmp_path: Path) -> Path:
    path = tmp_path / "idp.pem"
    path.write_text(RSAKeyPair.generate().public_key)
    return path


def test_from_env_reads_auth_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONTOS_AUTH_MODE", "jwt")
    monkeypatch.setenv("ONTOS_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("ONTOS_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("ONTOS_AUTH_JWKS_URI", f"{ISSUER}/jwks")
    monkeypatch.setenv("ONTOS_AUTH_REQUIRED_SCOPES", " ontos.read , ,ontos.ask")
    monkeypatch.setenv("ONTOS_ALLOW_UNAUTHENTICATED_BIND", "1")

    cfg = RuntimeConfig.from_env()

    assert cfg.auth_mode == "jwt"
    assert cfg.auth_issuer == ISSUER
    assert cfg.auth_audience == AUDIENCE
    assert cfg.auth_jwks_uri == f"{ISSUER}/jwks"
    assert cfg.auth_required_scopes == ("ontos.read", "ontos.ask")
    assert cfg.allow_unauthenticated_bind is True


def test_auth_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONTOS_AUTH_MODE", raising=False)
    monkeypatch.delenv("ONTOS_ALLOW_UNAUTHENTICATED_BIND", raising=False)

    cfg = RuntimeConfig.from_env()

    assert cfg.auth_mode == "none"
    assert cfg.allow_unauthenticated_bind is False
    assert build_token_verifier(cfg) is None


def test_unknown_auth_mode_rejected() -> None:
    with pytest.raises(AuthConfigError):
        parse_auth_mode("oauth-magic")


def test_jwt_mode_builds_verifier_from_pinned_key(public_key_file: Path) -> None:
    cfg = _cfg(
        auth_mode="jwt",
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_public_key_file=public_key_file,
    )

    assert isinstance(build_token_verifier(cfg), JWTVerifier)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"auth_issuer": None}, "ONTOS_AUTH_ISSUER"),
        ({"auth_audience": None}, "ONTOS_AUTH_AUDIENCE"),
        ({"auth_public_key_file": None}, "exactly one"),
        ({"auth_jwks_uri": f"{ISSUER}/jwks"}, "exactly one"),
    ],
)
def test_jwt_mode_rejects_incomplete_config(
    public_key_file: Path, overrides: dict[str, object], message: str
) -> None:
    base: dict[str, object] = {
        "auth_mode": "jwt",
        "auth_issuer": ISSUER,
        "auth_audience": AUDIENCE,
        "auth_public_key_file": public_key_file,
    }
    cfg = _cfg(**{**base, **overrides})

    with pytest.raises(AuthConfigError, match=message):
        build_token_verifier(cfg)


def test_jwks_uri_must_be_https() -> None:
    cfg = _cfg(
        auth_mode="jwt",
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_jwks_uri="http://idp.customer.internal/jwks",
    )

    with pytest.raises(AuthConfigError, match="https"):
        build_token_verifier(cfg)


def test_unreadable_public_key_file_fails_loud(tmp_path: Path) -> None:
    cfg = _cfg(
        auth_mode="jwt",
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_public_key_file=tmp_path / "missing.pem",
    )

    with pytest.raises(AuthConfigError, match="ONTOS_AUTH_PUBLIC_KEY_FILE"):
        build_token_verifier(cfg)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.9", "::1", "[::1]", "localhost"])
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "ontos.internal"])
def test_non_loopback_hosts(host: str) -> None:
    assert not is_loopback_host(host)


def test_unauthenticated_loopback_bind_allowed() -> None:
    enforce_bind_policy(auth_mode="none", host="127.0.0.1", allow_unauthenticated_bind=False)


def test_unauthenticated_public_bind_refused() -> None:
    with pytest.raises(UnauthenticatedBindError, match="ONTOS_ALLOW_UNAUTHENTICATED_BIND"):
        enforce_bind_policy(auth_mode="none", host="0.0.0.0", allow_unauthenticated_bind=False)


def test_unauthenticated_public_bind_allowed_with_opt_in() -> None:
    enforce_bind_policy(auth_mode="none", host="0.0.0.0", allow_unauthenticated_bind=True)


def test_authenticated_public_bind_allowed() -> None:
    enforce_bind_policy(auth_mode="jwt", host="0.0.0.0", allow_unauthenticated_bind=False)


def test_none_mode_passes_asserted_identity_through() -> None:
    caller = resolve_caller(
        mode="none",
        claimed_agent_identity="agent:a",
        claimed_principal="user:alice",
        token=None,
    )

    assert (caller.agent_identity, caller.principal, caller.method) == (
        "agent:a",
        "user:alice",
        "none",
    )
    assert caller.policy_decision() == {
        "policy": "authn",
        "method": "none",
        "result": "asserted",
    }


def test_jwt_mode_requires_token() -> None:
    with pytest.raises(CallerRejectedError) as exc:
        resolve_caller(
            mode="jwt", claimed_agent_identity="agent:a", claimed_principal=None, token=None
        )
    assert exc.value.reason == "missing_token"


def test_jwt_mode_requires_subject() -> None:
    with pytest.raises(CallerRejectedError) as exc:
        resolve_caller(
            mode="jwt",
            claimed_agent_identity="agent:a",
            claimed_principal=None,
            token=_token({"iss": ISSUER}),
        )
    assert exc.value.reason == "token_missing_subject"


def test_jwt_mode_principal_comes_from_subject() -> None:
    caller = resolve_caller(
        mode="jwt",
        claimed_agent_identity="agent:claimed",
        claimed_principal=None,
        token=_token({"sub": "user:alice", "iss": ISSUER}),
    )

    assert caller.principal == "user:alice"
    assert caller.agent_identity == "client:agent"
    assert caller.policy_decision() == {
        "policy": "authn",
        "method": "jwt",
        "subject": "user:alice",
        "agent": "client:agent",
        "issuer": ISSUER,
        "result": "allow",
        "claimed_agent": "agent:claimed",
    }


def test_jwt_mode_agent_comes_from_rfc8693_actor_claim() -> None:
    caller = resolve_caller(
        mode="jwt",
        claimed_agent_identity="agent:copilot",
        claimed_principal="user:alice",
        token=_token({"sub": "user:alice", "iss": ISSUER, "act": {"sub": "agent:copilot"}}),
    )

    assert caller.principal == "user:alice"
    assert caller.agent_identity == "agent:copilot"
    assert "claimed_agent" not in caller.policy_decision()


def test_jwt_mode_rejects_principal_that_disagrees_with_token() -> None:
    with pytest.raises(CallerRejectedError) as exc:
        resolve_caller(
            mode="jwt",
            claimed_agent_identity="agent:a",
            claimed_principal="user:bob",
            token=_token({"sub": "user:alice", "iss": ISSUER}),
        )
    assert exc.value.reason == "principal_mismatch"
