"""Single source of truth for the Ontos version.

Every place that needs to know the version reads it from here:

- `ontos/__init__.py` re-exports `__version__`.
- `pyproject.toml` declares `dynamic = ["version"]` and hatchling
  reads this file.
- `deploy/helm/ontos/Chart.yaml`'s `appVersion` is bumped manually
  during the release process to match — the RELEASING runbook
  documents the sequence.

Do not import anything from other Ontos modules in this file. It
gets read at build time before the package is installed.
"""

__version__ = "0.3.0"
