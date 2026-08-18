import pytest
from datetime import datetime, timezone

from app.config import ClimateRoomConfig, EntitiesConfig
from app.ha_client import HomeAssistantClient
from app.tools import register_tools
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError


def _payload(result: tuple) -> dict:
    return result[1]


class FakeHAClient:
    def __init__(self, states: dict[str, dict], errors: dict[str, str] | None = None) -> None:
        self.states = states
        self.errors = errors or {}

    async def get_state(self, entity_id: str) -> dict:
        if entity_id in self.errors:
            from app.ha_client import HomeAssistantError

            raise HomeAssistantError(self.errors[entity_id])
        if entity_id not in self.states:
            from app.ha_client import HomeAssistantError

            raise HomeAssistantError("Home Assistant 404: Entity not found")
        return self.states[entity_id]

    async def get_history_period(
        self,
        start_time,
        end_time=None,
        *,
        filter_entity_id=None,
        minimal_response=True,
        no_attributes=True,
    ):
        history = self.states.get(f"history:{filter_entity_id}")
        if history is None:
            from app.ha_client import HomeAssistantError

            raise HomeAssistantError("Home Assistant 404: Entity not found")
        return [history]


@pytest.mark.asyncio
async def test_battery_summary_detects_low_and_unknown() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    client = FakeHAClient(
        states={
            "sensor.ok": {"state": "18", "attributes": {"unit_of_measurement": "%"}},
            "sensor.unknownish": {"state": "unknown", "attributes": {}},
        }
    )
    entities = EntitiesConfig(
        batteries={"sensor_ok": "sensor.ok", "sensor_unknown": "sensor.unknownish"},
        allowed_raw_entities=["sensor.ok", "sensor.unknownish"],
    )
    register_tools(mcp, client, entities)

    result = await mcp.call_tool("ha_get_battery_summary", {"low_battery_threshold": 25})
    payload = _payload(result)

    assert payload["summary"]["total_batteries"] == 2
    assert payload["summary"]["low_batteries"] == 1
    assert payload["summary"]["unavailable_or_unknown"] == 1
    assert payload["low_battery_aliases"] == ["sensor_ok"]
    assert payload["batteries"]["sensor_ok"]["low_battery"] is True
    assert payload["batteries"]["sensor_unknown"]["low_battery"] is None


@pytest.mark.asyncio
async def test_ups_summary_ignores_disallowed_entities() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    client = FakeHAClient(states={})
    entities = EntitiesConfig(
        ups={"status": "sensor.allowed", "runtime": "sensor.not_allowed"},
        allowed_raw_entities=["sensor.allowed"],
    )
    register_tools(mcp, client, entities)

    result = await mcp.call_tool("ha_get_ups_summary", {})
    payload = _payload(result)

    assert payload["ups"]["status"] is None
    assert payload["ups"]["runtime"] is None


@pytest.mark.asyncio
async def test_climate_and_home_health_tolerate_missing_sensors() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    client = FakeHAClient(
        states={
            "sensor.temp": {
                "state": "24.1",
                "attributes": {"unit_of_measurement": "C", "friendly_name": "Temp"},
            },
            "sensor.humidity": {
                "state": "40",
                "attributes": {"unit_of_measurement": "%", "friendly_name": "Humidity"},
            },
            "sensor.ups_status": {"state": "Online", "attributes": {}},
            "sensor.nas_cpu": {"state": "11", "attributes": {"unit_of_measurement": "%"}},
        },
        errors={"sensor.missing_battery": "Home Assistant 404: Entity not found"},
    )
    entities = EntitiesConfig(
        climate={
            "sample_room": ClimateRoomConfig(
                temperature="sensor.temp",
                humidity="sensor.humidity",
                battery="sensor.missing_battery",
            )
        },
        batteries={"sample_room": "sensor.missing_battery"},
        infra={"nas": {"cpu": "sensor.nas_cpu"}},
        ups={"status": "sensor.ups_status", "runtime": None},
        allowed_raw_entities=["sensor.temp", "sensor.humidity", "sensor.missing_battery", "sensor.ups_status", "sensor.nas_cpu"],
    )
    register_tools(mcp, client, entities)

    climate_result = await mcp.call_tool("ha_get_climate_summary", {})
    home_health_result = await mcp.call_tool("ha_get_home_health_summary", {})

    climate_payload = _payload(climate_result)
    home_health_payload = _payload(home_health_result)

    assert climate_payload["climate"]["sample_room"]["temperature"]["state"] == "24.1"
    assert climate_payload["climate"]["sample_room"]["battery"] is None
    assert home_health_payload["ups"]["status"]["state"] == "Online"
    assert home_health_payload["infra"]["nas"]["cpu"]["state"] == "11"
    assert home_health_payload["batteries"]["summary"]["unavailable_or_unknown"] == 1


@pytest.mark.asyncio
async def test_list_allowed_entities_includes_new_sections() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    ha = HomeAssistantClient("https://home-assistant.example.invalid", "token")
    entities = EntitiesConfig(
        climate={"sample_room": ClimateRoomConfig(temperature="sensor.temp")},
        batteries={"sample_sensor": "sensor.battery"},
        lights={
            "sample_lamp": {
                "entity_id": "light.example_lamp",
                "friendly_name": "Example lamp",
                "aliases": ["demo lamp", "reading lamp"],
            }
        },
        ups={"status": "sensor.ups_status"},
        allowed_raw_entities=["sensor.temp", "sensor.battery", "sensor.ups_status", "light.example_lamp"],
    )
    register_tools(mcp, ha, entities)

    result = await mcp.call_tool("ha_list_allowed_entities", {})
    payload = _payload(result)

    assert "climate" in payload
    assert "batteries" in payload
    assert "ups" in payload
    assert payload["lights"]["sample_lamp"]["aliases"] == ["demo lamp", "reading lamp"]


@pytest.mark.asyncio
async def test_temperature_accepts_climate_location_and_summary_reports_extremes() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    client = FakeHAClient(
        states={
            "sensor.example_room_a_temperature": {"state": "28.4", "attributes": {"unit_of_measurement": "°C"}},
            "sensor.example_room_a_humidity": {"state": "41", "attributes": {"unit_of_measurement": "%"}},
            "sensor.example_room_b_temperature": {"state": "30.2", "attributes": {"unit_of_measurement": "°C"}},
            "sensor.example_room_b_humidity": {"state": "53", "attributes": {"unit_of_measurement": "%"}},
        }
    )
    entities = EntitiesConfig(
        rooms={"sample_room_a": {"temperature": "sensor.example_room_a_temperature"}},
        climate={
            "sample_room_a": {"temperature": "sensor.example_room_a_temperature", "humidity": "sensor.example_room_a_humidity"},
            "sample_room_b": {"temperature": "sensor.example_room_b_temperature", "humidity": "sensor.example_room_b_humidity"},
        },
        allowed_raw_entities=[
            "sensor.example_room_a_temperature",
            "sensor.example_room_a_humidity",
            "sensor.example_room_b_temperature",
            "sensor.example_room_b_humidity",
        ],
    )
    register_tools(mcp, client, entities)

    temperature_result = await mcp.call_tool("ha_get_temperature", {"room": "sample_room_b"})
    climate_result = await mcp.call_tool("ha_get_climate_summary", {})

    temperature_payload = _payload(temperature_result)
    climate_payload = _payload(climate_result)

    assert temperature_payload["entity_id"] == "sensor.example_room_b_temperature"
    assert climate_payload["summary"]["locations_with_temperature"] == 2
    assert climate_payload["summary"]["hottest"]["location"] == "sample_room_b"
    assert climate_payload["summary"]["hottest"]["temperature"] == 30.2
    assert climate_payload["summary"]["coolest"]["location"] == "sample_room_a"


@pytest.mark.asyncio
async def test_overnight_temperature_summary_calculates_min_max_and_average() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    client = FakeHAClient(
        states={
            "sensor.example_room_temperature": {"state": "28.0", "attributes": {"unit_of_measurement": "°C"}},
            "history:sensor.example_room_temperature": [
                {"state": "29.6", "last_changed": "2026-07-11T00:42:00+02:00"},
                {"state": "27.1", "last_changed": "2026-07-11T07:08:00+02:00"},
                {"state": "29.7", "last_changed": "2026-07-11T03:15:00+02:00"},
            ],
        }
    )
    entities = EntitiesConfig(
        rooms={"sample_room": {"temperature": "sensor.example_room_temperature"}},
        climate={"sample_room": {"temperature": "sensor.example_room_temperature"}},
        allowed_raw_entities=["sensor.example_room_temperature"],
    )
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.tools.datetime", FixedDatetime)
    try:
        register_tools(mcp, client, entities)

        result = await mcp.call_tool("ha_get_overnight_temperature", {"room": "sample_room"})
        payload = _payload(result)
    finally:
        monkeypatch.undo()

    assert payload == {
        "room": "sample_room",
        "period_start": "2026-07-10T23:00:00",
        "period_end": "2026-07-11T08:00:00",
        "min_temperature": 27.1,
        "min_time": "07:08",
        "max_temperature": 29.7,
        "max_time": "03:15",
        "average_temperature": 28.8,
    }


@pytest.mark.asyncio
async def test_overnight_temperature_summary_rejects_invalid_room() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    client = FakeHAClient(states={})
    entities = EntitiesConfig()
    register_tools(mcp, client, entities)

    with pytest.raises(ToolError):
        await mcp.call_tool("ha_get_overnight_temperature", {"room": "unknown_room"})


@pytest.mark.asyncio
async def test_overnight_temperature_summary_rejects_missing_history() -> None:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    client = FakeHAClient(states={"sensor.example_room_temperature": {"state": "28.0", "attributes": {}}})
    entities = EntitiesConfig(
        rooms={"sample_room": {"temperature": "sensor.example_room_temperature"}},
        climate={"sample_room": {"temperature": "sensor.example_room_temperature"}},
        allowed_raw_entities=["sensor.example_room_temperature"],
    )
    register_tools(mcp, client, entities)

    with pytest.raises(ToolError):
        await mcp.call_tool("ha_get_overnight_temperature", {"room": "sample_room"})
