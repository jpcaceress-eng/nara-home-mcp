from __future__ import annotations

import inspect
import json
from datetime import timedelta

import pytest
from mcp.server.fastmcp import FastMCP

from app.configuration import EntitiesConfig
from app.tools import register_tools


def payload(result: tuple) -> dict:
    return result[1]


class FakeHA:
    def __init__(self) -> None:
        self.states = [
            {"entity_id": "sensor.example_outdoor_pressure", "state": "1014", "attributes": {"friendly_name": "Example outdoor pressure", "unit_of_measurement": "hPa", "device_class": "atmospheric_pressure", "token_count": 7}},
            {"entity_id": "sensor.example_outdoor_temperature", "state": "22", "attributes": {"unit_of_measurement": "°C"}},
            {"entity_id": "sensor.example_outdoor_humidity", "state": "44", "attributes": {"unit_of_measurement": "%"}},
            {"entity_id": "lock.example_entry", "state": "locked", "attributes": {"friendly_name": "Example entry"}},
        ]
        self.history_calls: list[dict] = []

    async def list_states(self):
        return list(self.states)

    async def get_state(self, entity_id: str):
        return next(item for item in self.states if item["entity_id"] == entity_id)

    async def get_history_period(self, start_time, end_time=None, *, filter_entity_id=None, minimal_response=True, no_attributes=True):
        self.history_calls.append({"start": start_time, "end": end_time, "ids": filter_entity_id, "minimal": minimal_response, "no_attributes": no_attributes})
        return [[{"entity_id": entity_id, "state": "1", "last_changed": start_time.isoformat(), "attributes": {"source": "recorder"}}] for entity_id in filter_entity_id.split(",")]

    async def get_logbook_period(self, start_time, end_time, *, entity_id=None):
        return [{"entity_id": entity_id or "lock.example_entry", "name": "Example entry", "message": "changed to locked", "when": start_time.isoformat()}]


class FakeWS:
    async def get_statistics_metadata(self, statistic_ids=None):
        return [{"statistic_id": "sensor.example_outdoor_pressure", "name": "Example outdoor pressure", "source": "recorder", "unit_of_measurement": "hPa", "statistics_unit_of_measurement": "hPa", "mean_type": 1, "has_sum": False}]

    async def get_statistics_during_period(self, statistic_ids, start_time, end_time, period):
        return {statistic_ids[0]: [{"start": 1, "end": 2, "mean": 1014.0, "min": 1012.0, "max": 1016.0, "sum": None, "change": 4.0}]}


@pytest.fixture
def setup_mcp() -> tuple[FastMCP, FakeHA]:
    mcp = FastMCP("4B.6", stateless_http=True, json_response=True)
    ha = FakeHA()
    register_tools(mcp, ha, EntitiesConfig(), automation_websocket=FakeWS())
    return mcp, ha


@pytest.mark.asyncio
async def test_discovers_outdoor_pressure_and_returns_every_attribute_without_allowlist(setup_mcp) -> None:
    mcp, _ = setup_mcp
    found = payload(await mcp.call_tool("ha_list_states", {"query": "outdoor pressure"}))
    assert [item["entity_id"] for item in found["entities"]] == ["sensor.example_outdoor_pressure"]
    state = payload(await mcp.call_tool("ha_get_state", {"entity_id": "sensor.example_outdoor_pressure"}))
    assert state["attributes"] == {"friendly_name": "Example outdoor pressure", "unit_of_measurement": "hPa", "device_class": "atmospheric_pressure", "token_count": 7}


@pytest.mark.asyncio
async def test_multientity_history_over_24_hours_is_unrestricted_and_fragmented(setup_mcp) -> None:
    mcp, ha = setup_mcp
    result = payload(await mcp.call_tool("ha_get_history", {"entity_ids": ["sensor.example_outdoor_pressure", "sensor.example_outdoor_temperature", "sensor.example_outdoor_humidity"], "start_time": "2026-08-14T00:00:00+00:00", "end_time": "2026-08-16T00:00:00+00:00"}))
    assert set(result["entities"]) == {"sensor.example_outdoor_pressure", "sensor.example_outdoor_temperature", "sensor.example_outdoor_humidity"}
    assert result["cursor"] is not None
    assert ha.history_calls[0]["end"] - ha.history_calls[0]["start"] == timedelta(hours=24)
    assert ha.history_calls[0]["minimal"] is False and ha.history_calls[0]["no_attributes"] is False


@pytest.mark.asyncio
async def test_history_keeps_points_when_ha_omits_repeated_entity_id() -> None:
    class MinimalHA(FakeHA):
        async def get_history_period(self, *args, **kwargs):
            return [[
                {"entity_id": "sensor.example_outdoor_pressure", "state": "1012", "last_changed": "2026-08-16T00:00:00+00:00", "attributes": {"unit_of_measurement": "hPa"}},
                {"state": "1013", "last_changed": "2026-08-16T01:00:00+00:00", "attributes": {"unit_of_measurement": "hPa"}},
            ]]

    mcp = FastMCP("4B.6 history", stateless_http=True, json_response=True)
    ha = MinimalHA()
    register_tools(mcp, ha, EntitiesConfig(), automation_websocket=FakeWS())
    result = payload(await mcp.call_tool("ha_get_history", {"entity_ids": ["sensor.example_outdoor_pressure"]}))
    assert len(result["entities"]["sensor.example_outdoor_pressure"]) == 2
    assert result["entities"]["sensor.example_outdoor_pressure"][1]["attributes"] == {"unit_of_measurement": "hPa"}


@pytest.mark.asyncio
async def test_previously_excluded_domain_statistics_and_logbook(setup_mcp) -> None:
    mcp, _ = setup_mcp
    lock = payload(await mcp.call_tool("ha_get_state", {"entity_id": "lock.example_entry"}))
    assert lock["state"] == "locked"
    stats = payload(await mcp.call_tool("ha_get_statistics", {"statistic_ids": ["sensor.example_outdoor_pressure"]}))
    assert stats["statistics"]["sensor.example_outdoor_pressure"][0] == {"start": 1, "end": 2, "mean": 1014.0, "min": 1012.0, "max": 1016.0, "sum": None, "change": 4.0}
    logbook = payload(await mcp.call_tool("ha_get_logbook", {"entity_id": "lock.example_entry"}))
    assert logbook["events"][0]["entity_id"] == "lock.example_entry"


@pytest.mark.asyncio
async def test_entity_added_after_startup_appears_without_restart(setup_mcp) -> None:
    mcp, ha = setup_mcp
    assert payload(await mcp.call_tool("ha_list_states", {"query": "late_entity"}))["total"] == 0
    ha.states.append({"entity_id": "update.late_entity", "state": "on", "attributes": {"installed_version": "1", "latest_version": "2"}})
    assert payload(await mcp.call_tool("ha_list_states", {"query": "late_entity"}))["entities"][0]["entity_id"] == "update.late_entity"


@pytest.mark.asyncio
async def test_only_real_credentials_are_redacted(setup_mcp) -> None:
    mcp, ha = setup_mcp
    credential_url = "https://" + "fixture-user" + ":" + "fixture-pass" + "@example.invalid/path"
    ha.states.append({"entity_id": "sensor.example_credentials", "state": "ok", "attributes": {"token": "fixture-secret", "authorization": "Bearer fixture.value.signature", "url": credential_url, "person_name": "Morgan Example", "latitude": 40.4}})
    result = payload(await mcp.call_tool("ha_get_state", {"entity_id": "sensor.example_credentials"}))
    serialized = json.dumps(result)
    assert "fixture-secret" not in serialized and "fixture-user:fixture-pass" not in serialized
    assert result["attributes"]["person_name"] == "Morgan Example"
    assert result["attributes"]["latitude"] == 40.4


def test_public_catalog_has_no_write_tools_or_rest_writes(setup_mcp) -> None:
    from app.clients.home_assistant import HomeAssistantClient

    mcp, _ = setup_mcp
    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert not names & {"ha_turn_on", "ha_turn_off", "ha_set_light_brightness", "ha_set_display_brightness", "ha_run_scene"}
    client_source = inspect.getsource(HomeAssistantClient)
    assert all(f'"{verb}"' not in client_source for verb in ("POST", "PUT", "PATCH", "DELETE"))
