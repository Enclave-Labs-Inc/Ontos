"""Pretty-print Ontos JSON output for the demo (ingest report, query hits, graph)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def _load(path: str) -> dict:
    raw = Path(path).read_text()
    return json.loads(raw[raw.find("{") : raw.rfind("}") + 1])


def ingest(path: str) -> None:
    report = _load(path)
    print(f"  documents: {report['documents_extracted']}/{report['documents_seen']} extracted")
    print(f"  facts written: {report['facts_written']}  (reaffirmed: {report['facts_reaffirmed']})")
    for error in report["errors"]:
        print(f"  ! {error['source_id']}: {error['reason']}")


def errors(path: str) -> None:
    print(len(_load(path)["errors"]))


def query(path: str) -> None:
    result = _load(path)
    hits = result["hits"]
    if not hits:
        print("  (no hits)")
    for hit in hits:
        fact = hit["fact"]
        print(
            f"  {fact['subject_id']} -{fact['predicate']}-> {fact['object_id']}"
            f"   [source: {fact['provenance']['source_id']}]"
        )
    for warning in result["warnings"]:
        print(f"  note: {warning}")


def graph(path: str) -> None:
    from ontos.storage.networkx_store import NetworkxStore

    async def run() -> None:
        scan = await NetworkxStore(path=Path(path)).scan()
        print(f"  entities ({len(scan.entities)}):")
        for entity in scan.entities:
            print(f"    {entity.id:28} {entity.type}")
        print(f"  facts ({len(scan.facts)}):")
        for fact in scan.facts:
            print(
                f"    {fact.subject_id} -{fact.predicate}-> {fact.object_id}"
                f"   [source: {fact.provenance.source_id}]"
            )

    asyncio.run(run())


if __name__ == "__main__":
    {"ingest": ingest, "errors": errors, "query": query, "graph": graph}[sys.argv[1]](sys.argv[2])
