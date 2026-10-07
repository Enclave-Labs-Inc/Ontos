"""Sovereignty guardrails for BRIDGE LLM backends.

Ontos's thesis is "sovereign by architecture — nothing leaves the
customer VPC. Deployable air-gapped." A BRIDGE backend is any adapter
that sends prompts or document text off-premises (OpenAI, Anthropic,
or any cloud-hosted model API). Shipping BRIDGE adapters alongside
SOVEREIGN ones is useful for pilots and non-regulated use cases, but
operators of in-VPC regulated deploys need loud signals so they can't
accidentally break sovereignty.

Two guards:

- `warn_bridge_backend_instantiated(backend_id)` — fires a structlog
  WARNING on every construction. Operators tailing logs see a line
  per BRIDGE instantiation; a dashboard can slice on
  `bridge-backend-instantiated` to alert.

- `enforce_prod_bridge_opt_in(backend_id)` — in `ONTOS_ENV=prod`,
  raises `SovereigntyError` unless `ONTOS_ALLOW_BRIDGE_BACKENDS=1`
  is also set. Mirrors the dev-signing-key refusal pattern already
  shipped in `ontos.runtime.server` — fail loud by default; require
  an explicit second opt-in to bypass.

Both are called from `AnthropicBackend.__init__` and
`OpenAIBackend.__init__`.
"""

from __future__ import annotations

import os

import structlog

log = structlog.get_logger()

_PROD_OPT_IN_ENV = "ONTOS_ALLOW_BRIDGE_BACKENDS"


class SovereigntyError(RuntimeError):
    """Raised when a BRIDGE backend would run under a sovereign-only posture.

    The default posture in `ONTOS_ENV=prod` is sovereign-only: BRIDGE
    backends refuse to instantiate so a regulated in-VPC deploy
    cannot accidentally send prompts off-prem. Operators who have an
    explicit business reason (and have disclosed the sub-processor
    to their customers) set ``ONTOS_ALLOW_BRIDGE_BACKENDS=1`` to
    bypass.

    Operators who hit this have either (a) picked the wrong backend
    for a regulated deploy, (b) forgotten to set the opt-in env on
    an intentional BRIDGE deploy, or (c) are testing in a prod-like
    environment and need to flip the env for the test window.
    """

    def __init__(self, *, backend_id: str) -> None:
        self.backend_id = backend_id
        super().__init__(
            f"BRIDGE backend {backend_id!r} refused to instantiate: "
            f"ONTOS_ENV=prod requires an explicit "
            f"{_PROD_OPT_IN_ENV}=1 opt-in because prompts and "
            "document text leave the customer VPC. Set the env var "
            "if the BRIDGE posture is intentional (and the "
            "sub-processor is disclosed to your customers), or "
            "switch to a sovereign backend like Ollama."
        )


def warn_bridge_backend_instantiated(backend_id: str) -> None:
    """Emit a structlog WARNING naming the BRIDGE backend being constructed.

    Fires every call — not deduped on backend_id — so an operator
    log-tailing a long-running process sees one line per actual
    construction (useful for debugging unexpected re-instantiation).
    Dashboards can filter on the `bridge-backend-instantiated` event
    name to alert on BRIDGE use in environments that shouldn't see
    any.
    """
    log.warning(
        "bridge-backend-instantiated",
        backend=backend_id,
        posture="BRIDGE",
        reason=(
            "prompts and document text leave the customer VPC on the "
            "way to the backend's hosted API. Not sovereign-by-"
            "architecture. Use a SOVEREIGN backend (e.g. Ollama) for "
            "in-VPC regulated deploys."
        ),
    )


def enforce_prod_bridge_opt_in(backend_id: str) -> None:
    """Refuse BRIDGE backend construction in prod without explicit opt-in.

    When `ONTOS_ENV=prod`, raises `SovereigntyError` unless the
    operator has also set `ONTOS_ALLOW_BRIDGE_BACKENDS=1`. Outside
    prod (dev / staging / unset env), this is a no-op — pilots and
    local development against cloud models stay convenient.

    The two-env pattern mirrors `ontos.runtime.server`'s dev-signing-
    key guard: a loud default-refusal with an explicit, discoverable
    bypass.
    """
    if os.environ.get("ONTOS_ENV") != "prod":
        return
    if os.environ.get(_PROD_OPT_IN_ENV) == "1":
        log.warning(
            "bridge-backend-prod-opt-in",
            backend=backend_id,
            reason=(
                f"{_PROD_OPT_IN_ENV}=1 bypasses the sovereign-by-"
                "default guard. Verify the sub-processor is "
                "disclosed to customers."
            ),
        )
        return
    raise SovereigntyError(backend_id=backend_id)
