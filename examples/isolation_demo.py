"""Show tenant isolation against a running server. No API key needed.

Speaks MCP directly, with no model in the loop. The same question is asked as
three different callers; the multi-tenant caller's total should be exactly the
sum of the other two.

    docker compose up --build          # in another terminal
    uv run python examples/isolation_demo.py
"""

from __future__ import annotations

import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER_URL = os.environ.get("QG_URL", "http://127.0.0.1:8765/mcp")
QUESTION = "SELECT sum(amount) AS total FROM customer_orders WHERE status = 'completed'"

# Each token is scoped to different tenants by the demo verifier.
CALLERS = [("tok_acme", "Acme only"), ("tok_globex", "Globex only"), ("tok_multi", "both")]


async def ask(token: str, sql: str) -> dict:
    """Open one MCP session as `token` and run one governed query."""
    async with streamablehttp_client(SERVER_URL, headers={"Authorization": f"Bearer {token}"}) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("qg_run_query", {"sql": sql})
            return json.loads(result.content[0].text)


async def main() -> None:
    print(f"Asking every caller the same question:\n  {QUESTION}\n")

    totals: dict[str, float] = {}
    for token, description in CALLERS:
        payload = await ask(token, QUESTION)
        if "error" in payload:
            print(f"  {token:12} → {payload['kind']}: {payload['error']}")
            continue
        total = float(payload["rows"][0][0])
        totals[token] = total
        print(f"  {token:12} ({description:11}) → ${total:>12,.2f}")

    if {"tok_acme", "tok_globex", "tok_multi"} <= totals.keys():
        summed = totals["tok_acme"] + totals["tok_globex"]
        matches = round(summed, 2) == round(totals["tok_multi"], 2)
        print(f"\n  acme + globex = ${summed:,.2f}   equals tok_multi: {matches}")
        print("\nNo prompt can change this: the tenant filter is added to the SQL")
        print("after the caller's query is parsed, and re-verified before execution.")

    # The same server refuses a query it cannot scope, and says why.
    print("\nAsking for something the governance layer will not allow:")
    refused = await ask("tok_acme", "DELETE FROM customer_orders")
    print(f"  kind={refused.get('kind')}: {refused.get('error', '')[:88]}")


if __name__ == "__main__":
    asyncio.run(main())
