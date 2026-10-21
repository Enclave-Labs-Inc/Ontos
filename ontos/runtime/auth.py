"""Caller authentication for the MCP surface (#69).

Two modes, selected by ``ONTOS_AUTH_MODE``:

- ``none`` — identities are *asserted*: ``agent_identity`` and
  ``acting_on_behalf_of`` are taken from tool arguments as before.
  Only safe on a loopback bind, so `enforce_bind_policy` refuses a
  non-loopback address unless ``ONTOS_ALLOW_UNAUTHENTICATED_BIND=1``.
- ``jwt`` — every HTTP request must carry a bearer JWT verified
  against a customer-configured issuer (pinned public key, or the
  issuer's JWKS over HTTPS). The principal comes from the token's
  ``sub``; the acting agent from the RFC 8693 ``act.sub`` claim,
  falling back to the token's client id. A caller-supplied
  ``acting_on_behalf_of`` that disagrees with the token is rejected:
  delegation must be minted by the IdP (token exchange), never
  claimed in a tool argument.

Rejections are audited twice over: requests the transport turns
away with 401/403 by `AuthRejectionAuditMiddleware`, and tool calls
that arrive without a verified token (e.g. in-process transports)
by the server's per-tool authentication step.
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

import structlog

if TYPE_CHECKING:
    from fastmcp.server.auth import AccessToken
    from fastmcp.server.auth.providers.jwt import JWTVerifier
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from ontos.audit.base import AuditSink
    from ontos.runtime.config import RuntimeConfig

log = structlog.get_logger()

AuthMode = Literal["none", "jwt"]
AUTH_MODES: tuple[AuthMode, ...] = ("none", "jwt")
UNAUTHENTICATED_BIND_OPT_IN_ENV = "ONTOS_ALLOW_UNAUTHENTICATED_BIND"


class AuthConfigError(RuntimeError):
    """The configured authentication settings are incomplete or unsafe."""


class UnauthenticatedBindError(RuntimeError):
    """Refused to expose an unauthenticated server beyond loopback."""

    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(
            f"refusing to bind {host!r} with ONTOS_AUTH_MODE=none: callers "
            "could assert any identity. Configure ONTOS_AUTH_MODE=jwt, bind "
            f"a loopback address, or set {UNAUTHENTICATED_BIND_OPT_IN_ENV}=1 "
            "if the network path is otherwise restricted (dev only)."
        )


class CallerRejectedError(Exception):
    """A tool call could not be tied to a verified identity.

    `reason` is a short machine-readable code that goes to the audit
    record only; callers receive a generic message so the response
    does not reveal which check failed.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CallerIdentity:
    """Who is calling, and how sure we are of it."""

    agent_identity: str
    principal: str | None
    method: AuthMode
    issuer: str | None = None
    claimed_agent_identity: str | None = None

    def policy_decision(self) -> dict[str, str]:
        """The `authn` entry recorded on every Article-12 record."""
        if self.method == "none":
            return {"policy": "authn", "method": "none", "result": "asserted"}
        decision = {
            "policy": "authn",
            "method": self.method,
            "subject": self.principal or "",
            "agent": self.agent_identity,
            "issuer": self.issuer or "",
            "result": "allow",
        }
        if (
            self.claimed_agent_identity is not None
            and self.claimed_agent_identity != self.agent_identity
        ):
            decision["claimed_agent"] = self.claimed_agent_identity
        return decision


def parse_auth_mode(raw: str) -> AuthMode:
    """Validate an ``ONTOS_AUTH_MODE`` value."""
    mode = raw.strip().lower() or "none"
    if mode == "none":
        return "none"
    if mode == "jwt":
        return "jwt"
    raise AuthConfigError(f"ONTOS_AUTH_MODE={raw!r} not supported. Use one of: {AUTH_MODES}.")


def build_token_verifier(config: RuntimeConfig) -> JWTVerifier | None:
    """Return the JWT verifier for `config`, or None when auth is off.

    Fails loud on any gap: a verifier without an issuer or audience
    would accept tokens minted for other services.
    """
    mode = parse_auth_mode(config.auth_mode)
    if mode == "none":
        return None

    if not config.auth_issuer:
        raise AuthConfigError("ONTOS_AUTH_MODE=jwt requires ONTOS_AUTH_ISSUER")
    if not config.auth_audience:
        raise AuthConfigError("ONTOS_AUTH_MODE=jwt requires ONTOS_AUTH_AUDIENCE")

    has_jwks = bool(config.auth_jwks_uri)
    has_key = config.auth_public_key_file is not None
    if has_jwks == has_key:
        raise AuthConfigError(
            "ONTOS_AUTH_MODE=jwt requires exactly one of ONTOS_AUTH_JWKS_URI "
            "or ONTOS_AUTH_PUBLIC_KEY_FILE"
        )

    public_key: str | None = None
    if config.auth_public_key_file is not None:
        try:
            public_key = config.auth_public_key_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise AuthConfigError(
                f"cannot read ONTOS_AUTH_PUBLIC_KEY_FILE: {exc.strerror}"
            ) from exc
    elif config.auth_jwks_uri is not None and urlparse(config.auth_jwks_uri).scheme != "https":
        # Key material fetched over plaintext could be swapped in transit,
        # letting an attacker mint tokens the server would accept.
        raise AuthConfigError("ONTOS_AUTH_JWKS_URI must use https")

    from fastmcp.server.auth.providers.jwt import JWTVerifier

    return JWTVerifier(
        public_key=public_key,
        jwks_uri=config.auth_jwks_uri or None,
        issuer=config.auth_issuer,
        audience=config.auth_audience,
        algorithm=config.auth_algorithm,
        required_scopes=list(config.auth_required_scopes) or None,
    )


def is_loopback_host(host: str) -> bool:
    """True when `host` only accepts connections from this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def enforce_bind_policy(*, auth_mode: str, host: str, allow_unauthenticated_bind: bool) -> None:
    """Refuse to serve unauthenticated MCP beyond loopback without an opt-in."""
    if parse_auth_mode(auth_mode) != "none" or is_loopback_host(host):
        return
    if not allow_unauthenticated_bind:
        raise UnauthenticatedBindError(host)
    log.warning(
        "unauthenticated-bind-allowed",
        host=host,
        opt_in=UNAUTHENTICATED_BIND_OPT_IN_ENV,
        message=(
            "MCP is reachable beyond loopback without authentication; any "
            "caller can assert any identity. Use ONTOS_AUTH_MODE=jwt outside dev."
        ),
    )


def resolve_caller(
    *,
    mode: str,
    claimed_agent_identity: str,
    claimed_principal: str | None,
    token: AccessToken | None,
) -> CallerIdentity:
    """Bind a tool call to an identity, or raise `CallerRejectedError`."""
    if parse_auth_mode(mode) == "none":
        return CallerIdentity(
            agent_identity=claimed_agent_identity,
            principal=claimed_principal,
            method="none",
        )

    if token is None:
        raise CallerRejectedError("missing_token")

    claims: dict[str, Any] = dict(token.claims)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise CallerRejectedError("token_missing_subject")

    if claimed_principal is not None and claimed_principal != subject:
        raise CallerRejectedError("principal_mismatch")

    actor = claims.get("act")
    actor_subject = actor.get("sub") if isinstance(actor, dict) else None
    agent = actor_subject if isinstance(actor_subject, str) and actor_subject else token.client_id
    issuer = claims.get("iss")

    return CallerIdentity(
        agent_identity=agent,
        principal=subject,
        method="jwt",
        issuer=issuer if isinstance(issuer, str) else None,
        claimed_agent_identity=claimed_agent_identity,
    )


class AuthRejectionAuditMiddleware:
    """ASGI middleware that audits requests the transport rejects.

    FastMCP answers a missing or invalid bearer token with 401 (or 403
    for insufficient scope) before any tool runs, so no tool-level
    audit record would exist for that attempt. This wraps the app and
    emits one Article-12 record per rejected request. The token itself
    is never recorded.
    """

    def __init__(self, app: ASGIApp, *, audit: AuditSink, model_versions: dict[str, str]) -> None:
        self.app = app
        self.audit = audit
        self.model_versions = model_versions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status: int | None = None

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                if status in (401, 403):
                    self._emit_rejection(scope, status, started)
            await send(message)

        await self.app(scope, receive, capture)

    def _emit_rejection(self, scope: Scope, status: int, started: float) -> None:
        has_token = any(name.lower() == b"authorization" for name, _ in scope.get("headers", []))
        if status == 403:
            reason = "insufficient_scope"
        else:
            reason = "invalid_token" if has_token else "missing_token"
        self.audit.emit(
            query_id=None,
            agent_identity="(unauthenticated)",
            acting_on_behalf_of=None,
            query_text="",
            tool_invoked="authn",
            tool_arguments={"method": scope.get("method", ""), "path": scope.get("path", "")},
            result_fact_ids=[],
            latency_ms=(time.perf_counter() - started) * 1000,
            model_versions=self.model_versions,
            policy_decisions=[
                {
                    "policy": "authn",
                    "method": "jwt",
                    "result": "deny",
                    "reason": reason,
                    "status": str(status),
                }
            ],
        )
