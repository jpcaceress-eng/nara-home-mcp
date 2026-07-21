from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from app.config import EntitiesConfig
from app.ha_client import HomeAssistantError
from app.tools import register_tools


def _payload(result: tuple) -> dict:
    return result[1]


class FakeRecentChangesClient:
    def __init__(
        self,
        histories: dict[str, list],
        states: dict[str, dict],
        *,
        error_entity: str | None = None,
    ) -> None:
        self.histories = histories
        self.states = states
        self.error_entity = error_entity
        self.requested_entities: list[str] = []

    async def get_state(self, entity_id: str) -> dict:
        self.requested_entities.append(entity_id)
        if entity_id == self.error_entity:
            raise HomeAssistantError("Home Assistant 503: unavailable")
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
        if filter_entity_id == self.error_entity:
            raise HomeAssistantError("Home Assistant 503: unavailable")
        return [self.histories.get(filter_entity_id, [])]


def _build_mcp(client: FakeRecentChangesClient, allowed: list[str]) -> FastMCP:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    register_tools(mcp, client, EntitiesConfig(allowed_raw_entities=allowed))
    return mcp


def _state(value: str, name: str, unit: str | None = None) -> dict:
    attributes = {"friendly_name": name, "secret": "do-not-expose"}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    return {"state": value, "attributes": attributes, "context": {"user_id": "private-user"}}


@pytest.mark.asyncio
async def test_recent_changes_returns_empty_when_no_entity_changed() -> None:
    client = FakeRecentChangesClient(
        histories={"sensor.one": [{"state": "stable", "last_changed": "2026-07-21T08:00:00+00:00"}]},
        states={"sensor.one": _state("stable", "One")},
    )
    mcp = _build_mcp(client, ["sensor.one"])

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload == {"period_hours": 12, "changes_found": 0, "changes": []}


@pytest.mark.asyncio
async def test_recent_changes_returns_one_changed_entity() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.temperature": [
                {"state": "20", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "21", "last_changed": "2026-07-21T09:00:00+00:00"},
            ]
        },
        states={"sensor.temperature": _state("21", "Room temperature", "°C")},
    )
    mcp = _build_mcp(client, ["sensor.temperature"])

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {"hours": 6}))

    assert payload["period_hours"] == 6
    assert payload["changes_found"] == 1
    assert payload["changes"] == [
        {
            "timestamp": "2026-07-21T09:00:00+00:00",
            "entity_id": "sensor.temperature",
            "friendly_name": "Room temperature",
            "old_value": "20",
            "new_value": "21",
            "unit": "°C",
        }
    ]


@pytest.mark.asyncio
async def test_recent_changes_combines_all_changes_and_orders_newest_first() -> None:
    client = FakeRecentChangesClient(
        histories={
            "binary_sensor.door": [
                {"state": "off", "last_changed": "2026-07-21T07:00:00+00:00"},
                {"state": "on", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "off", "last_changed": "2026-07-21T10:00:00+00:00"},
            ],
            "sensor.temperature": [
                {"state": "19", "last_changed": "2026-07-21T07:30:00+00:00"},
                {"state": "20", "last_changed": "2026-07-21T09:00:00+00:00"},
            ],
        },
        states={
            "binary_sensor.door": _state("off", "Door"),
            "sensor.temperature": _state("20", "Temperature", "°C"),
        },
    )
    mcp = _build_mcp(client, ["binary_sensor.door", "sensor.temperature"])

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["changes_found"] == 3
    assert [change["timestamp"] for change in payload["changes"]] == [
        "2026-07-21T10:00:00+00:00",
        "2026-07-21T09:00:00+00:00",
        "2026-07-21T08:00:00+00:00",
    ]
    assert [change["entity_id"] for change in payload["changes"]] == [
        "binary_sensor.door",
        "sensor.temperature",
        "binary_sensor.door",
    ]


@pytest.mark.asyncio
async def test_recent_changes_filters_attributes_and_only_queries_allowlisted_entities() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.allowed": [
                {"state": "1", "last_changed": "2026-07-21T08:00:00+00:00"},
                {
                    "state": "2",
                    "last_changed": "2026-07-21T09:00:00+00:00",
                    "attributes": {"secret": "history-secret"},
                    "context": {"user_id": "history-user"},
                },
            ],
            "sensor.forbidden": [
                {"state": "private", "last_changed": "2026-07-21T09:30:00+00:00"},
            ],
        },
        states={
            "sensor.allowed": _state("2", "Allowed"),
            "sensor.forbidden": _state("private", "Forbidden"),
        },
    )
    mcp = _build_mcp(client, ["sensor.allowed"])

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))
    serialized = json.dumps(payload)

    assert client.requested_entities == ["sensor.allowed"]
    assert "sensor.forbidden" not in serialized
    assert "do-not-expose" not in serialized
    assert "history-secret" not in serialized
    assert "private-user" not in serialized
    assert "history-user" not in serialized
    assert "attributes" not in serialized
    assert "context" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [0, 169])
async def test_recent_changes_enforces_the_hour_limit(hours: int) -> None:
    client = FakeRecentChangesClient(
        histories={"sensor.one": []},
        states={"sensor.one": _state("stable", "One")},
    )
    mcp = _build_mcp(client, ["sensor.one"])

    with pytest.raises(ToolError, match="hours must be between 1 and 168"):
        await mcp.call_tool("ha_get_recent_changes", {"hours": hours})

    assert client.requested_entities == []


@pytest.mark.asyncio
async def test_recent_changes_propagates_home_assistant_errors_safely() -> None:
    client = FakeRecentChangesClient(
        histories={"sensor.one": []},
        states={"sensor.one": _state("stable", "One")},
        error_entity="sensor.one",
    )
    mcp = _build_mcp(client, ["sensor.one"])

    with pytest.raises(ToolError, match="Home Assistant 503"):
        await mcp.call_tool("ha_get_recent_changes", {})
