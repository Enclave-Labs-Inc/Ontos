"""Sovereignty guardrails on BRIDGE LLM backends.

Pins two invariants:

1. Every BRIDGE backend instantiation fires a loud structlog warning
   so operators tailing logs can see "sovereignty just broke" and a
   dashboard can alert on the event name.
2. In `ONTOS_ENV=prod`, BRIDGE backends refuse to instantiate without
   an explicit `ONTOS_ALLOW_BRIDGE_BACKENDS=1` opt-in. Mirrors the
   dev-signing-key default-refuse pattern in `ontos.runtime.server`.

Both the AnthropicBackend and the OpenAIBackend go through the same
guards; the tests are parametrized so a future BRIDGE backend lands
with the same contract or the suite fails.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import structlog

from ontos.llm._sovereignty import (
    SovereigntyError,
    enforce_prod_bridge_opt_in,
    warn_bridge_backend_instantiated,
)
from ontos.llm.anthropic import AnthropicBackend
from ontos.llm.openai import OpenAIBackend


def _construct_anthropic() -> object:
    # api_key is deferred — ctor doesn't need the key present.
    return AnthropicBackend(model="claude-haiku-4-5", client=object(), api_key=None)


def _construct_openai() -> object:
    return OpenAIBackend(model="gpt-4o-mini", client=object(), api_key=None)


BRIDGE_BACKENDS: list[tuple[str, Callable[[], object]]] = [
    ("anthropic", _construct_anthropic),
    ("openai", _construct_openai),
]


@pytest.mark.parametrize("backend_name,construct", BRIDGE_BACKENDS)
def test_bridge_backend_construction_emits_structlog_warning(
    backend_name: str, construct: Callable[[], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bridge-backend-instantiated` must fire on every BRIDGE backend
    construction. Operators log-tailing a server see one line per
    actual instantiation; a dashboard can slice on the event name."""
    monkeypatch.delenv("ONTOS_ENV", raising=False)
    with structlog.testing.capture_logs() as logs:
        construct()
    bridge_events = [log for log in logs if log["event"] == "bridge-backend-instantiated"]
    assert len(bridge_events) == 1, bridge_events
    event = bridge_events[0]
    assert event["posture"] == "BRIDGE"
    assert event["backend"].startswith(f"{backend_name}:")


@pytest.mark.parametrize("backend_name,construct", BRIDGE_BACKENDS)
def test_bridge_backend_refuses_in_prod_without_opt_in(
    backend_name: str, construct: Callable[[], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ONTOS_ENV=prod` without `ONTOS_ALLOW_BRIDGE_BACKENDS=1` raises
    `SovereigntyError`. Message names the backend and tells the operator
    both bypass paths (set the opt-in, or switch to Ollama)."""
    monkeypatch.setenv("ONTOS_ENV", "prod")
    monkeypatch.delenv("ONTOS_ALLOW_BRIDGE_BACKENDS", raising=False)
    with pytest.raises(SovereigntyError) as excinfo:
        construct()
    assert excinfo.value.backend_id.startswith(f"{backend_name}:")
    assert "ONTOS_ALLOW_BRIDGE_BACKENDS" in str(excinfo.value)
    assert "Ollama" in str(excinfo.value) or "sovereign" in str(excinfo.value).lower()


@pytest.mark.parametrize("backend_name,construct", BRIDGE_BACKENDS)
def test_bridge_backend_allowed_in_prod_with_explicit_opt_in(
    backend_name: str, construct: Callable[[], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ONTOS_ALLOW_BRIDGE_BACKENDS=1` in prod lets BRIDGE through, but
    the opt-in itself is logged (`bridge-backend-prod-opt-in`) so an
    operator can audit the deliberate posture break."""
    monkeypatch.setenv("ONTOS_ENV", "prod")
    monkeypatch.setenv("ONTOS_ALLOW_BRIDGE_BACKENDS", "1")
    with structlog.testing.capture_logs() as logs:
        backend = construct()
    assert backend is not None
    opt_in_events = [log for log in logs if log["event"] == "bridge-backend-prod-opt-in"]
    assert len(opt_in_events) == 1, opt_in_events
    assert opt_in_events[0]["backend"].startswith(f"{backend_name}:")
    # And the generic bridge-backend-instantiated warning still fires.
    bridge_events = [log for log in logs if log["event"] == "bridge-backend-instantiated"]
    assert len(bridge_events) == 1


def test_bridge_backend_allowed_outside_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev / staging / unset env: no refusal, just the loud warning.
    The default developer path (local Ollama for sovereignty testing,
    cloud for actual LLM evaluation) must stay convenient."""
    monkeypatch.setenv("ONTOS_ENV", "dev")
    monkeypatch.delenv("ONTOS_ALLOW_BRIDGE_BACKENDS", raising=False)
    _construct_anthropic()  # must not raise
    _construct_openai()


def test_sovereignty_error_names_the_backend_id() -> None:
    """The error message must carry the specific backend_id so an
    operator reading a stack trace sees which backend tripped the
    guard — matters when multiple BRIDGE adapters are in flight."""
    exc = SovereigntyError(backend_id="anthropic:claude-opus-5")
    assert "anthropic:claude-opus-5" in str(exc)
    assert exc.backend_id == "anthropic:claude-opus-5"


def test_warn_helper_fires_once_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """`warn_bridge_backend_instantiated` is not deduped by id — each
    call emits one warning. A long-running process re-instantiating a
    backend (test teardowns, process forks) sees one line per actual
    construction, which is the operator-useful signal."""
    monkeypatch.delenv("ONTOS_ENV", raising=False)
    with structlog.testing.capture_logs() as logs:
        warn_bridge_backend_instantiated("anthropic:claude-opus-5")
        warn_bridge_backend_instantiated("anthropic:claude-opus-5")
    bridge_events = [log for log in logs if log["event"] == "bridge-backend-instantiated"]
    assert len(bridge_events) == 2


def test_enforce_is_noop_outside_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirmation that the enforce helper does nothing when
    ONTOS_ENV isn't exactly 'prod' — staging, test, unset all pass
    through without raising or logging."""
    for env_value in ("dev", "staging", "test"):
        monkeypatch.setenv("ONTOS_ENV", env_value)
        enforce_prod_bridge_opt_in("anthropic:x")  # must not raise
    monkeypatch.delenv("ONTOS_ENV", raising=False)
    enforce_prod_bridge_opt_in("anthropic:x")  # must not raise
