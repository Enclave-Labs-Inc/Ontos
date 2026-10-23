"""Fixtures shared by the MCP authentication compliance tests."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair

from ontos.authz import VIEW, InMemoryAuthz
from ontos.runtime.config import RuntimeConfig
from ontos.runtime.models import Fact
from ontos.storage.networkx_store import NetworkxStore
from tests.compliance._mcp_harness import AUDIENCE, ISSUER, make_fact


@pytest.fixture
def key_pair() -> RSAKeyPair:
    return RSAKeyPair.generate()


@pytest.fixture
def jwt_config(tmp_path: Path, key_pair: RSAKeyPair) -> RuntimeConfig:
    key_file = tmp_path / "idp.pem"
    key_file.write_text(key_pair.public_key)
    return dataclasses.replace(
        RuntimeConfig.from_env(),
        auth_mode="jwt",
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_public_key_file=key_file,
    )


@pytest.fixture
async def finance_and_legal_store() -> tuple[NetworkxStore, Fact, Fact]:
    store = NetworkxStore()
    finance = make_fact("Acme Finance", "acl:finance")
    legal = make_fact("Acme Legal", "acl:legal")
    await store.add_fact(finance)
    await store.add_fact(legal)
    return store, finance, legal


@pytest.fixture
def authz() -> InMemoryAuthz:
    authz = InMemoryAuthz()
    authz.grant("user:alice", VIEW, "acl:finance")
    authz.grant("user:bob", VIEW, "acl:legal")
    return authz
