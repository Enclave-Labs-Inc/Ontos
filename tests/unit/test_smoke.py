"""M0 smoke tests — the package imports, models validate, minimal wiring holds."""

from __future__ import annotations

import ontos
from ontos.runtime.models import ArticleTwelveField, Confidence, Fact


def test_package_imports() -> None:
    # Don't hardcode the version — it bumps every release and this test
    # would rot. Just check the shape (SemVer major.minor.patch, with an
    # optional pre-release / build metadata trailer).
    import re

    assert re.match(r"^\d+\.\d+\.\d+([-.+][A-Za-z0-9.-]+)?$", ontos.__version__)


def test_article_twelve_has_twelve_fields() -> None:
    # The whole compliance pitch rests on this. If someone drops a field, fail loud.
    assert len(list(ArticleTwelveField)) == 12


def test_fact_is_frozen(sample_fact: Fact) -> None:
    import pydantic

    try:
        sample_fact.predicate = "changed"  # type: ignore[misc]
    except pydantic.ValidationError:
        return
    raise AssertionError("Fact should be frozen — mutation must raise")


def test_confidence_enum_values() -> None:
    assert Confidence.EXTRACTED.value == "EXTRACTED"
    assert Confidence.INFERRED.value == "INFERRED"
    assert Confidence.AMBIGUOUS.value == "AMBIGUOUS"
