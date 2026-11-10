"""What an AI agent sees when it talks to Ontos over MCP."""

from __future__ import annotations

import asyncio
import sys

from fastmcp import Client

WHO = {"agent_identity": "demo-agent", "acting_on_behalf_of": "user:demo"}


def _facts(payload: list[dict]) -> None:
    for fact in payload:
        provenance = fact["provenance"]
        print(
            f"    {fact['subject_id']} -{fact['predicate']}-> {fact['object_id']}"
            f"   [source: {provenance['source_id']}, model: {provenance['extractor_version']},"
            f" confidence: {provenance['confidence_score']}]"
        )


async def main(url: str) -> None:
    async with Client(url) as client:
        tools = [tool.name for tool in await client.list_tools()]
        print(f"  tools available to the agent: {', '.join(tools)}")

        print("\n  explain('Acme Corp') - everything known about Acme, with sources:")
        explained = await client.call_tool("explain", {"entity_id": "Company:acme corp", **WHO})
        _facts(explained.data.payload)

        print("\n  search('Beta') - keyword search across the graph:")
        searched = await client.call_tool("search", {"query": "Beta", "k": 5, **WHO})
        _facts(searched.data.payload)

        audit = await client.call_tool(
            "audit_lookup", {"query_id": str(explained.data.query_id), **WHO}
        )
        record = audit.data.payload["article12"]
        print("\n  Article-12 audit record of the explain call:")
        for key in ("tool_invoked", "agent_identity", "acting_on_behalf_of", "timestamp"):
            print(f"    {key}: {record[key]}")
        print(f"    result_fact_ids: {record['result_fact_ids']}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"))
