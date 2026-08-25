"""Ask QueryGate a question in plain English, with Claude writing the SQL.

    docker compose up --build                 # in another terminal
    export ANTHROPIC_API_KEY=...              # or: ant auth login
    uv run --with 'anthropic[mcp]' python examples/agent_loop.py \
        "How much completed revenue do we have, by region?"
"""

from __future__ import annotations

import asyncio
import os
import sys

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER_URL = os.environ.get("QG_URL", "http://127.0.0.1:8765/mcp")
# Swap this for tok_globex and re-run the same question: different numbers,
# same code, no prompt change.
TOKEN = os.environ.get("QG_TOKEN", "tok_acme")

DEFAULT_QUESTION = "How much completed revenue do we have, by region?"


async def main(question: str) -> None:
    client = AsyncAnthropic()

    async with streamablehttp_client(SERVER_URL, headers={"Authorization": f"Bearer {TOKEN}"}) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as mcp_session:
            init = await mcp_session.initialize()
            listed = await mcp_session.list_tools()
            print(f"connected to {SERVER_URL} as {TOKEN}")
            print(f"tools: {', '.join(t.name for t in listed.tools)}\n")

            # The server ships its own workflow guidance (discover before
            # querying, use real filter values, answer in business language).
            # Passing it through means this script holds no prompt of its own.
            runner = client.beta.messages.tool_runner(
                model="claude-opus-4-8",
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=init.instructions or "",
                messages=[{"role": "user", "content": question}],
                tools=[async_mcp_tool(tool, mcp_session) for tool in listed.tools],
            )

            async for message in runner:
                for block in message.content:
                    if block.type == "text" and block.text.strip():
                        print(block.text)
                    elif block.type == "tool_use":
                        # Worth watching: the agent discovers the schema before
                        # it writes any SQL, because guessing gets rejected.
                        print(f"  → {block.name}")


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or DEFAULT_QUESTION
    print(f"Q: {question}\n")
    asyncio.run(main(question))
