import asyncio

from app.config import EntitiesConfig
from app.ha_client import HomeAssistantClient
from app.tools import register_tools
from mcp.server.fastmcp import FastMCP


def test_registered_tool_inventory_includes_infra_tools() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    ha = HomeAssistantClient("http://ha.local", "token")
    entities = EntitiesConfig()
    register_tools(mcp, ha, entities)

    async def get_names() -> list[str]:
        tools = await mcp.list_tools()
        return sorted(tool.name for tool in tools)

    names = asyncio.run(get_names())

    assert names == [
        "ha_get_battery_summary",
        "ha_get_climate_summary",
        "ha_get_entity_history",
        "ha_get_home_health_summary",
        "ha_get_infra_summary",
        "ha_get_nas_summary",
        "ha_get_overnight_temperature",
        "ha_get_presence",
        "ha_get_proxmox_summary",
        "ha_get_recent_changes",
        "ha_get_server_version",
        "ha_get_state",
        "ha_get_temperature",
        "ha_get_ups_summary",
        "ha_list_allowed_entities",
        "ha_run_scene",
        "ha_set_display_brightness",
        "ha_set_light_brightness",
        "ha_turn_off",
        "ha_turn_on",
    ]

    asyncio.run(ha.aclose())
