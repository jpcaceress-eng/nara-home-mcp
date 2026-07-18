from __future__ import annotations

import os

import anyio


async def main() -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp")
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            for tool in sorted(tools.tools, key=lambda item: item.name):
                print(tool.name)


if __name__ == "__main__":
    anyio.run(main)
