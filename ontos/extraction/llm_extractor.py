"""LLM-backed extractor.

This is the M1 concrete `Extractor` implementation. It takes an LLM
backend (satisfying `LLMBackend` — a narrow surface any provider can
adapt to) and an `Ontology`, and produces `ExtractionResult`s whose
every fact carries full `Provenance`.

Design divergence from `docs/design/m1.md`
------------------------------------------
The design doc called the file `llama_index.py` and described it as
"Wraps neo4j-graphrag-python's LLMEntityRelationExtractor." In
implementation we found the neo4j-graphrag class doesn't give us
enough control over the confidence/provenance path to be worth
subclassing — the wrapper has to reach past its public API for every
enrichment we care about. Instead, this module defines a small
`LLMBackend` protocol that any provider can adapt to (OpenAI,
Anthropic, neo4j-graphrag's `LLMInterface`, and eventually Scribe),
and implements the extractor pipeline directly. The design intent
(schema-strict, provenance-per-fact, loud-fail on silent-empty,
Scribe-drop-in) is preserved.

The `Extractor` Protocol from `base.py` is unchanged, so nothing
downstream cares which shape the impl took.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field

from ontos.extraction.base import (
    CandidateProvenance,
    CandidateTriple,
    ExtractionError,
    ExtractionInput,
    ExtractionResult,
    RejectedCandidate,
)
from ontos.ontology import Ontology
from ontos.runtime.models import Confidence, Entity, Fact, Provenance

log = structlog.get_logger()


class RawTriple(BaseModel):
    """One triple emitted by an LLM before ontos-side calibration.

    The LLM reports its own confidence (0..1); the extractor maps that
    into a Confidence label + calibrated score. Entity ids are strings
    the LLM chooses — `LlmExtractor.extract` requires them to be stable
    within a single call so multiple triples about the same subject
    dedupe correctly.
    """

    subject_id: str
    subject_type: str
    subject_canonical_name: str
    predicate: str
    object_id: str
    object_type: str
    object_canonical_name: str
    llm_confidence: float = Field(ge=0.0, le=1.0)

    model_config = ConfigDict(frozen=True)


@runtime_checkable
class LLMBackend(Protocol):
    """The narrow LLM interface the extractor needs.

    Any provider adapts to this — an OpenAI wrapper, an Anthropic
    wrapper, an adapter around neo4j-graphrag's `LLMInterface`, or (in
    the near future) an Enclave Scribe client. The extractor doesn't
    care which; it only needs structured output.
    """

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def structured_extract(
        self,
        text: str,
        ontology: Ontology,
    ) -> list[RawTriple]: ...


def _normalize_canonical_name(entity_type: str, canonical_name: str) -> str:
    """Remove LLM-added entity-type decoration from a display name.

    Raw entity ids remain untouched. This only protects canonical_name,
    whose contract is the human-readable entity name.
    """
    name = canonical_name.strip()

    prefix = f"{entity_type}:"
    if name.casefold().startswith(prefix.casefold()):
        stripped = name[len(prefix) :].strip()
        if stripped:
            name = stripped

    suffix = f"({entity_type})"
    if name.casefold().endswith(suffix.casefold()):
        stripped = name[: -len(suffix)].strip()
        if stripped:
            name = stripped

    return name


def _calibrate(raw: float) -> tuple[Confidence, float]:
    """Map raw LLM confidence to (Confidence label, calibrated score).

    Simple threshold mapping for M1. A trained calibrator is a
    Scribe-era concern per the design doc.
    """
    if raw >= 0.9:
        return Confidence.EXTRACTED, raw
    if raw >= 0.6:
        return Confidence.INFERRED, raw
    return Confidence.AMBIGUOUS, raw


class LlmExtractor:
    """Extractor implementation backed by an `LLMBackend`.

    Enforces the design invariants:

    - Schema-strict: every triple must match a declared `Pattern` in
      the ontology. Non-matches are dropped and surfaced as warnings —
      this is the concrete implementation of `additional_node_types=False`.
    - Provenance-first: every returned `Fact` carries a full
      `Provenance` populated from `input.source_id`, `self.id`,
      `self.version`, the calibrated confidence label, and the raw
      LLM confidence as the score.
    - Loud fail: on non-empty input where the LLM returns zero triples,
      raises `ExtractionError` rather than emitting a silently-empty
      `ExtractionResult`. This is the M1.a-baked defense against the
      LangChain silent-empty bug documented in the Round-4 research.
    - Bitemporal: `t_valid` and `ingested_at` default to `now(UTC)`;
      callers with stronger claims (e.g. filing dates) can shift them
      after the fact via supersession.
    """

    def __init__(
        self,
        llm: LLMBackend,
        ontology: Ontology,
        *,
        min_confidence: float = 0.5,
    ) -> None:
        self._llm = llm
        self._ontology = ontology
        self._min_confidence = min_confidence

    @property
    def id(self) -> str:
        return f"ontos.llm_extractor+{self._llm.id}"

    @property
    def version(self) -> str:
        return self._llm.version

    async def extract(self, input: ExtractionInput) -> ExtractionResult:
        # Wrap backend exceptions as ExtractionError so IngestPipeline's
        # existing ErrorPolicy (FAIL_FAST / SKIP_AND_LOG) handles them
        # cleanly instead of leaking raw httpx / provider stack traces.
        # OllamaTimeoutError gets a dedicated branch so its message (which
        # already names --ollama-timeout) survives verbatim; other
        # exceptions get a generic wrap. Parallels LlmPlanner.plan().
        try:
            raw_triples = await self._llm.structured_extract(input.text, self._ontology)
        except ExtractionError:
            raise
        except Exception as exc:
            # Never log the exception message, traceback, or source text.
            log.warning(
                "llm-backend-failed",
                source_id=input.source_id,
                error_type=type(exc).__name__,
            )
            # Deferred import to avoid pulling the Ollama module into every
            # extractor construction (keeps the Scribe-drop-in boundary
            # clean — this except-case is Ollama-specific sugar, not an
            # ontology-wide dependency).
            from ontos.llm.ollama import OllamaTimeoutError

            if isinstance(exc, OllamaTimeoutError):
                raise ExtractionError(
                    source_id=input.source_id,
                    message=str(exc),
                    sample=input.text,
                    kind="backend",
                ) from exc
            raise ExtractionError(
                source_id=input.source_id,
                message="LLM backend failed",
                sample=input.text,
                kind="backend",
            ) from exc

        if not raw_triples and input.text.strip():
            raise ExtractionError(
                source_id=input.source_id,
                message="LLM returned zero triples for non-empty input",
                sample=input.text,
                kind="empty",
            )

        warnings: list[str] = []
        rejected: list[RejectedCandidate] = []
        entities: dict[str, Entity] = {}
        facts: list[Fact] = []
        raw_identity_by_id: dict[str, tuple[str, str]] = {}
        now = datetime.now(UTC)

        for raw in raw_triples:
            label, score = _calibrate(raw.llm_confidence)

            if not self._ontology.is_valid_pattern(
                raw.subject_type, raw.predicate, raw.object_type
            ):
                reason: Literal["pattern_not_in_ontology", "below_min_confidence"] | None = (
                    "pattern_not_in_ontology"
                )
                warning = "Triple rejected: pattern not in the ontology."
            elif score < self._min_confidence:
                reason = "below_min_confidence"
                warning = "Triple rejected: below min_confidence."
            else:
                reason = None

            if reason is not None:
                rejected.append(
                    RejectedCandidate(
                        raw_triple=CandidateTriple.model_validate(raw.model_dump()),
                        reason=reason,
                        provenance=CandidateProvenance(
                            source_id=input.source_id,
                            extractor_id=self.id,
                            extractor_version=self.version,
                            ontology_id=self._ontology.id,
                            ontology_version=self._ontology.version,
                            confidence_score=score,
                        ),
                    )
                )
                warnings.append(warning)
                continue

            provenance = Provenance(
                source_id=input.source_id,
                extractor_id=self.id,
                extractor_version=self.version,
                ontology_id=self._ontology.id,
                ontology_version=self._ontology.version,
                confidence=label,
                confidence_score=score,
            )

            for eid, etype, ename in (
                (raw.subject_id, raw.subject_type, raw.subject_canonical_name),
                (raw.object_id, raw.object_type, raw.object_canonical_name),
            ):
                canonical_name = _normalize_canonical_name(etype, ename)
                identity_key = (
                    etype.strip().casefold(),
                    canonical_name.casefold(),
                )

                previous_identity = raw_identity_by_id.get(eid)
                if previous_identity is not None and previous_identity != identity_key:
                    raise ExtractionError(
                        source_id=input.source_id,
                        message=(
                            f"raw entity id {eid!r} maps to multiple identities "
                            "within one extraction response"
                        ),
                        sample=input.text,
                    )

                raw_identity_by_id[eid] = identity_key

                if eid not in entities:
                    entities[eid] = Entity(
                        id=eid,
                        type=etype,
                        canonical_name=canonical_name,
                        provenance=provenance,
                    )

            facts.append(
                Fact(
                    subject_id=raw.subject_id,
                    predicate=raw.predicate,
                    object_id=raw.object_id,
                    provenance=provenance,
                    t_valid=now,
                    ingested_at=now,
                )
            )

        return ExtractionResult(
            entities=list(entities.values()),
            facts=facts,
            rejected=rejected,
            warnings=warnings,
        )
