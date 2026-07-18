import pytest

from app.config import EntitiesConfig
from app.tools import register_tools
from mcp.server.fastmcp import FastMCP


class FakeHAClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict | None]] = []

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict | None = None,
    ) -> list[dict]:
        self.calls.append((domain, service, entity_id, service_data))
        return [{"entity_id": entity_id, "state": "on"}]


@pytest.mark.asyncio
async def test_light_tools_accept_alias_and_entity_id() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    client = FakeHAClient()
    entities = EntitiesConfig(
        lights={
            "sample_lamp": {
                "entity_id": "light.example_lamp",
                "friendly_name": "Example lamp",
                "aliases": ["demo lamp", "reading lamp", "sample light"],
            }
        }
    )
    register_tools(mcp, client, entities)

    await mcp.call_tool("ha_turn_on", {"name": "reading lamp"})
    await mcp.call_tool("ha_turn_off", {"name": "light.example_lamp"})
    await mcp.call_tool("ha_set_light_brightness", {"name": "sample light", "brightness_pct": 50})

    assert client.calls[0][2] == "light.example_lamp"
    assert client.calls[1][2] == "light.example_lamp"
    assert client.calls[2][2] == "light.example_lamp"
    assert client.calls[2][3] == {"brightness": 128}
