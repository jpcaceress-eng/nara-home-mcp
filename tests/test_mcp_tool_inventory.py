from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from app.clients import HomeAssistantClient
from app.configuration import EntitiesConfig
from app.configuration_files import HomeAssistantConfigProvider
from app.inventory import InventoryCollector, InventoryNormalizer, InventoryStore
from app.tools import register_tools


EXPECTED_PUBLIC_TOOLS = {
    "ha_analyze_automation", "ha_diagnose_automation_trace",
    "ha_find_broken_automation_references", "ha_find_entity_usage",
    "ha_get_automation_config", "ha_get_automation_edit_proposal",
    "ha_get_automation_trace", "ha_get_automation_yaml", "ha_get_battery_summary",
    "ha_get_climate_summary", "ha_get_dependencies", "ha_get_device", "ha_get_history",
    "ha_get_home_health_summary", "ha_get_infra_summary", "ha_get_logbook",
    "ha_get_nas_summary", "ha_get_overnight_temperature", "ha_get_presence",
    "ha_get_proxmox_summary", "ha_get_server_version", "ha_get_state",
    "ha_get_statistics", "ha_get_temperature", "ha_get_ups_summary",
    "ha_inventory_status", "ha_list_allowed_entities", "ha_list_automation_traces",
    "ha_list_automations", "ha_list_automations_detailed", "ha_list_config_text_files",
    "ha_list_dashboards", "ha_list_states", "ha_list_yaml_files",
    "ha_prepare_automation_edit", "ha_read_config_text_file", "ha_read_dashboard",
    "ha_read_yaml_file", "ha_scan_entity_health", "ha_search_automations",
    "ha_search_config_text_files", "ha_search_inventory", "ha_search_yaml_files",
}


def test_public_snapshot_registers_exactly_43_read_only_tools() -> None:
    class FakeWebSocket:
        async def connect(self) -> None: ...

    mcp = FastMCP(name="Nara Home public catalog", stateless_http=True, json_response=True)
    ha = HomeAssistantClient("https://home-assistant.example.invalid", "fixture-token")
    websocket = FakeWebSocket()
    store = InventoryStore(
        InventoryCollector(websocket),  # type: ignore[arg-type]
        InventoryNormalizer(),
    )
    register_tools(
        mcp,
        ha,
        EntitiesConfig(),
        automation_websocket=websocket,  # type: ignore[arg-type]
        inventory_store=store,
        ha_config_provider=HomeAssistantConfigProvider(enabled=False),
    )

    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert names == EXPECTED_PUBLIC_TOOLS
    assert len(names) == 43
    assert not names & {
        "ha_turn_on", "ha_turn_off", "ha_set_light_brightness",
        "ha_set_display_brightness", "ha_run_scene", "ha_call_service",
    }
    asyncio.run(ha.aclose())


def test_server_metadata_declares_write_capability_false() -> None:
    mcp = FastMCP(name="Nara Home metadata", stateless_http=True, json_response=True)
    ha = HomeAssistantClient("https://home-assistant.example.invalid", "fixture-token")
    register_tools(mcp, ha, EntitiesConfig())
    result = asyncio.run(mcp.call_tool("ha_get_server_version", {}))
    assert result[1]["write_capability"] is False
    asyncio.run(ha.aclose())
