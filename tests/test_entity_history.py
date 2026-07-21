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


class FakeHistoryClient:
    def __init__(
        self,
        history: object,
        *,
        current: dict | None = None,
        error: str | None = None,
    ) -> None:
        self.history = history
        self.current = current or {"state": "current", "attributes": {}}
        self.error = error
        self.history_calls: list[dict] = []

    async def get_state(self, entity_id: str) -> dict:
        if self.error:
            raise HomeAssistantError(self.error)
        return self.current

    async def get_history_period(
        self,
        start_time,
        end_time=None,
        *,
        filter_entity_id=None,
        minimal_response=True,
        no_attributes=True,
    ):
        if self.error:
            raise HomeAssistantError(self.error)
        self.history_calls.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "filter_entity_id": filter_entity_id,
                "minimal_response": minimal_response,
                "no_attributes": no_attributes,
            }
        )
        return self.history


def _build_mcp(client: FakeHistoryClient, allowed: list[str] | None = None) -> FastMCP:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    register_tools(
        mcp,
        client,
        EntitiesConfig(allowed_raw_entities=allowed or ["sensor.allowed"]),
    )
    return mcp


@pytest.mark.asyncio
async def test_entity_history_queries_an_allowed_entity() -> None:
    client = FakeHistoryClient(
        [[{"state": "20", "last_changed": "2026-07-21T08:00:00+00:00"}]],
        current={"state": "21", "attributes": {"unit_of_measurement": "°C"}},
    )
    mcp = _build_mcp(client)

    payload = _payload(await mcp.call_tool("ha_get_entity_history", {"entity_id": "sensor.allowed"}))

    assert payload["entity_id"] == "sensor.allowed"
    assert payload["period"]["hours"] == 6
    assert payload["current_or_last_known_state"] == "21"
    assert payload["unit"] == "°C"
    assert client.history_calls[0]["filter_entity_id"] == "sensor.allowed"
    assert client.history_calls[0]["minimal_response"] is True
    assert client.history_calls[0]["no_attributes"] is True


@pytest.mark.asyncio
async def test_entity_history_rejects_a_disallowed_entity() -> None:
    client = FakeHistoryClient([])
    mcp = _build_mcp(client)

    with pytest.raises(ToolError, match="not allowed"):
        await mcp.call_tool("ha_get_entity_history", {"entity_id": "sensor.forbidden"})

    assert client.history_calls == []


@pytest.mark.asyncio
async def test_entity_history_returns_a_stable_empty_result() -> None:
    mcp = _build_mcp(FakeHistoryClient([], current={"state": "idle", "attributes": {}}))

    payload = _payload(await mcp.call_tool("ha_get_entity_history", {"entity_id": "sensor.allowed", "hours": 1}))

    assert payload["history_available"] is False
    assert payload["incomplete"] is False
    assert payload["truncated"] is False
    assert payload["changes"] == []
    assert payload["current_or_last_known_state"] == "idle"


@pytest.mark.asyncio
async def test_entity_history_normalizes_and_orders_state_changes() -> None:
    history = [[
        {"state": "closed", "last_changed": "2026-07-21T10:10:00+00:00"},
        {"state": "open", "last_changed": "2026-07-21T09:00:00+00:00"},
        {"state": "open", "last_changed": "2026-07-21T09:30:00+00:00"},
    ]]
    mcp = _build_mcp(FakeHistoryClient(history))

    payload = _payload(await mcp.call_tool("ha_get_entity_history", {"entity_id": "sensor.allowed"}))

    assert payload["changes"] == [
        {
            "timestamp": "2026-07-21T09:00:00+00:00",
            "previous_value": None,
            "new_value": "open",
        },
        {
            "timestamp": "2026-07-21T10:10:00+00:00",
            "previous_value": "open",
            "new_value": "closed",
        },
    ]


@pytest.mark.asyncio
async def test_entity_history_filters_unnecessary_attributes() -> None:
    client = FakeHistoryClient(
        [[
            {
                "entity_id": "sensor.allowed",
                "state": "10",
                "last_changed": "2026-07-21T08:00:00+00:00",
                "attributes": {"secret": "do-not-expose", "friendly_name": "Private name"},
                "context": {"user_id": "private-user"},
            }
        ]],
        current={
            "state": "10",
            "attributes": {"unit_of_measurement": "%", "secret": "do-not-expose"},
            "context": {"user_id": "private-user"},
        },
    )
    mcp = _build_mcp(client)

    payload = _payload(await mcp.call_tool("ha_get_entity_history", {"entity_id": "sensor.allowed"}))
    serialized = json.dumps(payload)

    assert payload["unit"] == "%"
    assert "do-not-expose" not in serialized
    assert "private-user" not in serialized
    assert "attributes" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [0, 169])
async def test_entity_history_enforces_the_hour_limit(hours: int) -> None:
    client = FakeHistoryClient([])
    mcp = _build_mcp(client)

    with pytest.raises(ToolError, match="hours must be between 1 and 168"):
        await mcp.call_tool("ha_get_entity_history", {"entity_id": "sensor.allowed", "hours": hours})

    assert client.history_calls == []


@pytest.mark.asyncio
async def test_entity_history_propagates_home_assistant_errors_safely() -> None:
    mcp = _build_mcp(FakeHistoryClient([], error="Home Assistant 503: unavailable"))

    with pytest.raises(ToolError, match="Home Assistant 503"):
        await mcp.call_tool("ha_get_entity_history", {"entity_id": "sensor.allowed"})


@pytest.mark.asyncio
async def test_entity_history_limits_the_number_of_changes() -> None:
    history = [[
        {
            "state": str(index),
            "last_changed": f"2026-07-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00",
        }
        for index in range(501)
    ]]
    mcp = _build_mcp(FakeHistoryClient(history))

    payload = _payload(await mcp.call_tool("ha_get_entity_history", {"entity_id": "sensor.allowed"}))

    assert payload["change_count"] == 500
    assert payload["truncated"] is True
    assert payload["incomplete"] is True
    assert payload["changes"][0]["previous_value"] == "0"
    assert payload["changes"][0]["new_value"] == "1"
