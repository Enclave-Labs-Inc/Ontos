"""OpenFGA adapter — the production authz backend.

Loaded only when `ONTOS_AUTHZ_BACKEND=openfga` is set. The dependency
lives in the `authz` optional extra so the base install doesn't pull
it in for `inmemory` / `none` deploys.

OpenFGA's model is `(user, relation, object)` tuples; ours maps
directly:

- **user**   = the `acting_on_behalf_of` identity supplied to the MCP
  tool (e.g. `user:alice`, `agent:bob`).
- **relation** = the string constant `VIEW` — Ontos does not currently
  distinguish read vs write vs approve; extending is a follow-up.
- **object** = the `acl_ref` string on each `Fact` (e.g. `acl:sensitive`).

We use OpenFGA's `ListObjects` API to bulk-resolve which ACLs a subject
can view; the server calls this once per request and passes the list
to the store, so filtering happens at query time in Cypher (no
per-edge RPC).
"""

from __future__ import annotations

import os
from typing import Any

from ontos.authz.base import AuthzError


class OpenFGAAuthz:
    """Adapter around `openfga-sdk`.

    Constructed via `from_env()`; direct instantiation is for tests
    that inject a mock client.
    """

    id: str = "openfga"

    def __init__(
        self,
        client: Any,
        store_id: str,
        authorization_model_id: str | None,
        object_type: str = "acl",
    ) -> None:
        self._client = client
        self._store_id = store_id
        self._model_id = authorization_model_id
        self._object_type = object_type

    @classmethod
    def from_env(cls) -> OpenFGAAuthz:
        try:
            from openfga_sdk import (  # type: ignore[import-not-found]
                ClientConfiguration,
                OpenFgaClient,
            )
        except ImportError as exc:  # pragma: no cover — exercised in prod deploys only
            raise AuthzError(
                "OpenFGA backend requires the `authz` extra: "
                "`uv sync --extra authz` or add `openfga-sdk` to your deploy image."
            ) from exc
        api_url = os.environ.get("FGA_API_URL")
        store_id = os.environ.get("FGA_STORE_ID")
        if not api_url or not store_id:
            raise AuthzError("OpenFGA requires FGA_API_URL and FGA_STORE_ID to be set")
        config_kwargs: dict[str, Any] = {"api_url": api_url, "store_id": store_id}
        api_token = os.environ.get("FGA_API_TOKEN")
        if api_token:
            # Bearer-token auth flag names differ across openfga-sdk minor
            # versions; the SDK gracefully ignores unknown kwargs, and the
            # in-VPC deploys we target usually run OpenFGA unauthenticated.
            config_kwargs["api_token"] = api_token
        config = ClientConfiguration(**config_kwargs)
        client = OpenFgaClient(config)
        return cls(
            client=client,
            store_id=store_id,
            authorization_model_id=os.environ.get("FGA_MODEL_ID"),
        )

    async def check(self, subject: str, relation: str, object_: str) -> bool:
        try:
            body = {"user": subject, "relation": relation, "object": object_}
            response = await self._client.check(body=body)
        except Exception as exc:  # noqa: BLE001 — see AuthzError contract
            raise AuthzError(f"OpenFGA check failed for {subject}/{relation}/{object_}") from exc
        allowed = getattr(response, "allowed", None)
        return bool(allowed)

    async def list_authorized_objects(self, subject: str, relation: str) -> list[str]:
        body = {
            "user": subject,
            "relation": relation,
            "type": self._object_type,
        }
        try:
            response = await self._client.list_objects(body=body)
        except Exception as exc:  # noqa: BLE001
            raise AuthzError(f"OpenFGA list_objects failed for {subject}/{relation}") from exc
        objects = getattr(response, "objects", []) or []
        return list(objects)
