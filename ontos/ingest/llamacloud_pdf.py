"""LlamaParsePdfConnector — PDF ingest via LlamaCloud's hosted vision API.

BRIDGE connector. Sends PDF bytes to ``api.cloud.llamaindex.ai`` for
parsing; the parsed markdown comes back and is fed to the extractor
unchanged. This is NOT sovereignty-safe — document bytes leave the
customer's VPC. It is the right choice for scanned / image-based PDFs
that a pure-text parser (``pypdf``) cannot read, which in practice
covers the majority of real-world document archives in finance,
pharma, and legal.

For in-VPC compliance, a sovereign ``PypdfConnector`` lands as a
follow-up (text-only PDFs; local parsing). The pair gives operators
the compliance trade-off explicitly: pick ``pypdf`` for sovereign;
pick ``llamaparse`` when the content needs vision.

The connector loads ``llama_cloud_services`` lazily so the base
install stays slim — install the extra to enable it::

    pip install 'enclave-ontos[llama]'
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog

from ontos.ingest.base import ConnectorError, SourceDocument


class _LlamaParseLike(Protocol):
    """Minimal shape of ``llama_cloud_services.LlamaParse`` we rely on."""

    async def aload_data(self, path: str) -> list[Any]: ...


log = structlog.get_logger(__name__)

# Module-level latch so the BRIDGE warning fires once per process, not
# once per instantiated connector or once per PDF.
_BRIDGE_WARNING_EMITTED = False

_LLAMACLOUD_HOST = "api.cloud.llamaindex.ai"


class LlamaParsePdfConnector:
    """Yields ``SourceDocument``s whose text is LlamaParse's markdown."""

    id: str = "ontos.ingest.llamaparse-connector"
    source_kind: str = "pdf-llamaparse"

    def __init__(
        self,
        pdf_paths: list[Path],
        *,
        api_key: str,
        root: Path | None = None,
        acl_ref_by_source: dict[str, str] | None = None,
        metadata_by_source: dict[str, dict[str, str]] | None = None,
        result_type: Literal["markdown", "text"] = "markdown",
    ) -> None:
        if not api_key:
            raise ConnectorError(
                "LlamaParsePdfConnector requires a non-empty api_key. "
                "Set LLAMA_CLOUD_API_KEY in your environment."
            )
        self._pdf_paths = list(pdf_paths)
        self._api_key = api_key
        self._root = root
        self._acl_refs = dict(acl_ref_by_source or {})
        self._metadata = dict(metadata_by_source or {})
        self._result_type = result_type
        _emit_bridge_warning_once(len(self._pdf_paths))

    def _source_id_for(self, path: Path) -> str:
        """Stable, collision-free document key.

        When ``root`` is set, use the path relative to it (matches what the
        text slice does in ``_read_source_dir`` so provenance from mixed
        directories is consistent). Falls back to the basename for callers
        that don't pass a root — collisions become the caller's problem to
        avoid, but the CLI always passes ``root``.
        """
        if self._root is not None:
            try:
                return path.relative_to(self._root).as_posix()
            except ValueError:
                # Path isn't actually under root — fall back to basename
                # rather than silently stripping, so this surfaces.
                pass
        return path.name

    async def iter_documents(self) -> AsyncIterator[SourceDocument]:
        parser = _load_parser(api_key=self._api_key, result_type=self._result_type)

        for path in self._pdf_paths:
            source_id = self._source_id_for(path)
            try:
                docs = await parser.aload_data(str(path))
            except Exception as exc:  # noqa: BLE001 — wrap everything as ConnectorError
                raise ConnectorError(f"LlamaParse failed to parse {source_id}: {exc!r}") from exc

            if not docs:
                raise ConnectorError(
                    f"LlamaParse returned zero documents for {source_id}. "
                    f"Try result_type='text' as a fallback."
                )

            text = "\n\n".join(getattr(d, "text", "") or "" for d in docs).strip()
            if not text:
                raise ConnectorError(
                    f"LlamaParse returned empty text for {source_id}. "
                    f"Try result_type='text' as a fallback."
                )

            caller_metadata = dict(self._metadata.get(source_id, {}))
            caller_metadata.setdefault("parser", "llamaparse")
            caller_metadata.setdefault("result_type", self._result_type)
            caller_metadata.setdefault("page_count", str(len(docs)))
            caller_metadata.setdefault("char_count", str(len(text)))
            # Record the RELATIVE path, not str(path): absolute paths would
            # leak customer directory layout + usernames into provenance /
            # audit records. source_id is already the relative path when
            # root is set, so this is redundant but kept for operators who
            # want "source_path" as a metadata key explicitly.
            caller_metadata.setdefault("source_path", source_id)

            yield SourceDocument(
                source_id=source_id,
                text=text,
                metadata=caller_metadata,
                acl_ref=self._acl_refs.get(source_id),
            )


def _load_parser(*, api_key: str, result_type: Literal["markdown", "text"]) -> _LlamaParseLike:
    """Import and construct LlamaParse lazily so the base install stays slim."""
    try:
        from llama_cloud_services import LlamaParse  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ConnectorError(
            "llama_cloud_services is not installed. Install the extra: "
            "pip install 'enclave-ontos[llama]'"
        ) from exc

    parser: _LlamaParseLike = LlamaParse(api_key=api_key, result_type=result_type, verbose=False)
    return parser


def _emit_bridge_warning_once(pdf_count: int) -> None:
    global _BRIDGE_WARNING_EMITTED
    if _BRIDGE_WARNING_EMITTED:
        return
    _BRIDGE_WARNING_EMITTED = True
    log.warning(
        "bridge-connector-in-use",
        connector="LlamaParsePdfConnector",
        destination_host=_LLAMACLOUD_HOST,
        pdf_count=pdf_count,
        message=(
            "LlamaParsePdfConnector sends PDF bytes to "
            f"{_LLAMACLOUD_HOST} — BRIDGE connector, not sovereignty-safe. "
            "Follow #39 for the in-VPC pypdf path."
        ),
    )


def _reset_bridge_warning_latch_for_tests() -> None:
    """Reset the module-level latch so tests can observe the first-use warning."""
    global _BRIDGE_WARNING_EMITTED
    _BRIDGE_WARNING_EMITTED = False
