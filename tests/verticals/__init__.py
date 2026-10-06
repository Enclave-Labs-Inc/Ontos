"""Vertical graded-question suites.

Each vertical ships:
- an ontology (`docs/ontology/examples/<vertical>.yaml`, from M7.a/b)
- a corpus (`docs/ontology/examples/<vertical>-corpus/`, from M7.c)
- a set of graded questions (`_<vertical>.py`, this package)
- a static-validation test module (`test_<vertical>_graded.py`)

Live execution of the graded questions against a real store + LLM
lands in a later PR behind a `pytest -m graded` opt-in marker. The
static suite here proves the questions are ontology-valid and
corpus-realizable — everything short of running extraction end-to-end.
"""
