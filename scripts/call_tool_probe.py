from __future__ import annotations

import json
import os

import anyio


async def main() -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp")
    tool_name = os.environ.get("TOOL_NAME", "ha_get_server_version")
    payload = json.loads(os.environ.get("TOOL_ARGS", "{}"))

    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, payload)
            print(result)


if __name__ == "__main__":
    anyio.run(main)
