"""Shared real-HTTP MCP harness for compliance tests (#69, #78)."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import uvicorn
from fastmcp import FastMCP
from starlette.middleware import Middleware

from ontos.audit.emitter import AuditEmitter
from ontos.runtime.auth import AuthRejectionAuditMiddleware
from ontos.runtime.models import AuditRecord, Confidence, Fact, Provenance

ISSUER = "https://idp.customer.internal"
AUDIENCE = "ontos"


def make_fact(subject: str, acl_ref: str) -> Fact:
    return Fact(
        id=uuid4(),
        subject_id=subject,
        predicate="acquired",
        object_id="Widget Inc",
        provenance=Provenance(
            source_id=f"doc:{acl_ref}",
            extractor_id="test",
            extractor_version="0",
            confidence=Confidence.EXTRACTED,
            confidence_score=0.99,
        ),
        t_valid=datetime(2025, 1, 1, tzinfo=UTC),
        ingested_at=datetime.now(UTC),
        acl_ref=acl_ref,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextlib.asynccontextmanager
async def serving(server: FastMCP, audit: AuditEmitter) -> AsyncIterator[str]:
    """Run `server` over real streamable HTTP, wired like `run_server`."""
    app = server.http_app(
        middleware=[
            Middleware(
                AuthRejectionAuditMiddleware,
                audit=audit,
                model_versions={"runtime": "test"},
            )
        ]
    )
    port = free_port()
    uvi = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(uvi.serve())
    while not uvi.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvi.should_exit = True
        await task


def audit_records(audit: AuditEmitter) -> list[AuditRecord]:
    return list(audit._chain)


def structured_payload(result: Any) -> dict[str, Any]:
    content = result.structured_content
    assert isinstance(content, dict)
    return content
