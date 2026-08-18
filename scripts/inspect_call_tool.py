import asyncio

from app.config import EntitiesConfig
from app.ha_client import HomeAssistantClient
from app.tools import register_tools
from mcp.server.fastmcp import FastMCP


async def main() -> None:
    mcp = FastMCP(name="Test", stateless_http=True, json_response=True)
    ha = HomeAssistantClient("https://home-assistant.example.invalid", "token")
    register_tools(mcp, ha, EntitiesConfig())
    result = await mcp.call_tool("ha_list_allowed_entities", {})
    print(type(result))
    print(repr(result))
    await ha.aclose()


asyncio.run(main())
