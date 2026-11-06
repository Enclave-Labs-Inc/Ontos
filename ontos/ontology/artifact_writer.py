"""Safe, non-overwriting ontology-version artifact generation.

This module prepares files for operator review. It does not approve
changes, activate an ontology, or modify stored graph facts.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from ontos.migration.records import MigrationRecord
from ontos.ontology.loader import Ontology
from ontos.ontology.versioning import build_next_ontology


def _stage_yaml(destination: Path, content: str) -> Path:
    """Write a complete temporary file beside its destination."""
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    return temporary


def write_ontology_version(
    *,
    base: Ontology,
    record: MigrationRecord,
    ontology_path: Path | str,
    migration_path: Path | str,
) -> Ontology:
    """Create a new ontology file and its identity migration record.

    Destinations must have existing parent directories. Neither target
    may exist. Each file is published atomically via a no-overwrite
    hard link from a complete temporary file.

    A failure attempts to remove only files created by this call.
    This is not a two-file atomic transaction for concurrent readers;
    operators must review both artifacts before activation.
    """
    updated = build_next_ontology(base, record)

    ontology_target = Path(ontology_path)
    migration_target = Path(migration_path)

    if ontology_target.resolve() == migration_target.resolve():
        raise ValueError("ontology and migration paths must be different")

    targets = (ontology_target, migration_target)

    for target in targets:
        if not target.parent.is_dir():
            raise FileNotFoundError(f"destination directory does not exist: {target.parent}")
        if os.path.lexists(target):
            raise FileExistsError(f"refusing to overwrite existing artifact: {target}")

    contents = (
        yaml.safe_dump(
            updated.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        ),
        yaml.safe_dump(
            record.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        ),
    )

    staged: list[Path] = []
    # Track the identity of each published inode so rollback does
    # not remove a different file that replaced our artifact.
    created: list[tuple[Path, int, int]] = []

    try:
        for target, content in zip(targets, contents, strict=True):
            staged.append(_stage_yaml(target, content))

        for temporary, target in zip(staged, targets, strict=True):
            # os.link fails if target appeared after our preflight check.
            # Unlike os.replace, it never overwrites an existing file.
            staged_stat = temporary.stat()
            os.link(temporary, target)
            created.append((target, staged_stat.st_dev, staged_stat.st_ino))

    except BaseException:
        for target, device, inode in reversed(created):
            try:
                current = target.lstat()
                if (current.st_dev, current.st_ino) == (device, inode):
                    target.unlink(missing_ok=True)
            except OSError:
                # Cleanup is best effort. Preserve the original error
                # rather than replacing it with a rollback failure.
                pass
        raise

    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)

    return updated
