from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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
        self.requested_history_entities: list[str] = []

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
        self.requested_history_entities.append(filter_entity_id)
        if filter_entity_id == self.error_entity:
            raise HomeAssistantError("Home Assistant 503: unavailable")
        return [self.histories.get(filter_entity_id, [])]


def _build_mcp(
    client: FakeRecentChangesClient,
    allowed: list[str],
    *,
    recent_changes: dict | None = None,
) -> FastMCP:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    register_tools(
        mcp,
        client,
        EntitiesConfig(
            allowed_raw_entities=allowed,
            recent_changes=recent_changes or {},
        ),
    )
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

    assert payload == {
        "period_hours": 12,
        "changes_found": 0,
        "changes_before_filtering": 0,
        "changes_after_filtering": 0,
        "discarded_by_threshold": 0,
        "discarded_by_debounce": 0,
        "discarded_as_reordered_collection": 0,
        "truncated": False,
        "truncated_count": 0,
        "changes": [],
    }


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


@pytest.mark.asyncio
async def test_recent_changes_filters_small_temperature_noise_but_keeps_real_change() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.temperature": [
                {"state": "28.2", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "28.3", "last_changed": "2026-07-21T08:01:00+00:00"},
                {"state": "29.0", "last_changed": "2026-07-21T08:02:00+00:00"},
            ]
        },
        states={"sensor.temperature": _state("29.0", "Temperature", "°C")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.temperature"],
        recent_changes={"numeric_delta_by_unit": {"°C": 0.3}},
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["changes_before_filtering"] == 2
    assert payload["discarded_by_threshold"] == 1
    assert payload["changes_after_filtering"] == 1
    assert payload["changes"][0]["old_value"] == "28.3"
    assert payload["changes"][0]["new_value"] == "29.0"


@pytest.mark.asyncio
async def test_recent_changes_debounces_repeated_humidity_oscillations() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.humidity": [
                {"state": "40", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "41", "last_changed": "2026-07-21T08:00:10+00:00"},
                {"state": "40.5", "last_changed": "2026-07-21T08:00:20+00:00"},
                {"state": "43", "last_changed": "2026-07-21T08:00:30+00:00"},
            ]
        },
        states={"sensor.humidity": _state("43", "Humidity", "%")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.humidity"],
        recent_changes={"debounce_seconds": 30},
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["discarded_by_debounce"] == 2
    assert payload["changes"] == [
        {
            "timestamp": "2026-07-21T08:00:30+00:00",
            "entity_id": "sensor.humidity",
            "friendly_name": "Humidity",
            "old_value": "40",
            "new_value": "43",
            "unit": "%",
        }
    ]


@pytest.mark.asyncio
async def test_recent_changes_discards_return_to_initial_value_within_debounce() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.humidity": [
                {"state": "40", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "41", "last_changed": "2026-07-21T08:00:10+00:00"},
                {"state": "40", "last_changed": "2026-07-21T08:00:20+00:00"},
            ]
        },
        states={"sensor.humidity": _state("40", "Humidity", "%")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.humidity"],
        recent_changes={"debounce_seconds": 30},
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["changes_after_filtering"] == 0
    assert payload["discarded_by_debounce"] == 2
    assert payload["changes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_value", "new_value"),
    [
        ("pihole, nara-mcp", "nara-mcp, pihole"),
        ('["pihole", "nara-mcp"]', '["nara-mcp", "pihole"]'),
    ],
)
async def test_recent_changes_discards_reordered_collections(old_value: str, new_value: str) -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.services": [
                {"state": old_value, "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": new_value, "last_changed": "2026-07-21T08:01:00+00:00"},
            ]
        },
        states={"sensor.services": _state(new_value, "Services")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.services"],
        recent_changes={"unordered_entities": ["sensor.services"]},
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["discarded_as_reordered_collection"] == 1
    assert payload["changes"] == []


@pytest.mark.asyncio
async def test_recent_changes_keeps_unparseable_unordered_and_non_numeric_values() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.mode": [
                {"state": "unknown", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "available", "last_changed": "2026-07-21T08:01:00+00:00"},
            ]
        },
        states={"sensor.mode": _state("available", "Mode", "°C")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.mode"],
        recent_changes={
            "numeric_delta_by_unit": {"°C": 100},
            "unordered_entities": ["sensor.mode"],
        },
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["discarded_by_threshold"] == 0
    assert payload["discarded_as_reordered_collection"] == 0
    assert payload["changes_found"] == 1


@pytest.mark.asyncio
async def test_recent_changes_enforces_global_max_results() -> None:
    history = [
        {"state": str(index), "last_changed": f"2026-07-21T08:0{index}:00+00:00"}
        for index in range(5)
    ]
    client = FakeRecentChangesClient(
        histories={"sensor.counter": history},
        states={"sensor.counter": _state("4", "Counter")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.counter"],
        recent_changes={"max_results": 2},
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["changes_before_filtering"] == 4
    assert payload["changes_after_filtering"] == 4
    assert payload["changes_found"] == 2
    assert payload["truncated"] is True
    assert payload["truncated_count"] == 2
    assert (
        payload["changes_before_filtering"]
        - payload["discarded_by_threshold"]
        - payload["discarded_by_debounce"]
        - payload["discarded_as_reordered_collection"]
        == payload["changes_after_filtering"]
    )
    assert [change["new_value"] for change in payload["changes"]] == ["4", "3"]


@pytest.mark.asyncio
async def test_recent_changes_configuration_does_not_affect_entity_history() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.temperature": [
                {"state": "28.2", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "28.3", "last_changed": "2026-07-21T08:01:00+00:00"},
            ]
        },
        states={"sensor.temperature": _state("28.3", "Temperature", "°C")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.temperature"],
        recent_changes={"numeric_delta_by_unit": {"°C": 0.3}, "max_results": 1},
    )

    payload = _payload(
        await mcp.call_tool(
            "ha_get_entity_history",
            {"entity_id": "sensor.temperature", "hours": 6},
        )
    )

    assert payload["change_count"] == 2
    assert payload["changes"][1]["previous_value"] == "28.2"
    assert payload["changes"][1]["new_value"] == "28.3"


@pytest.mark.asyncio
async def test_recent_changes_does_not_truncate_by_default_above_200_changes() -> None:
    start = datetime(2026, 7, 21, 8, tzinfo=timezone.utc)
    history = [
        {
            "state": str(index),
            "last_changed": (start + timedelta(seconds=index)).isoformat(),
        }
        for index in range(202)
    ]
    client = FakeRecentChangesClient(
        histories={"sensor.counter": history},
        states={"sensor.counter": _state("201", "Counter")},
    )
    mcp = _build_mcp(client, ["sensor.counter"])

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["changes_before_filtering"] == 201
    assert payload["changes_after_filtering"] == 201
    assert payload["changes_found"] == 201
    assert payload["truncated"] is False
    assert payload["truncated_count"] == 0


@pytest.mark.asyncio
async def test_recent_changes_entity_threshold_takes_priority_and_exact_delta_is_kept() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.storage": [
                {"state": "1", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "1.5", "last_changed": "2026-07-21T08:01:00+00:00"},
            ]
        },
        states={"sensor.storage": _state("1.5", "Storage", "%")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.storage"],
        recent_changes={
            "numeric_delta_default": 10,
            "numeric_delta_by_unit": {"%": 5},
            "numeric_delta_by_entity": {"sensor.storage": 0.5},
        },
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["discarded_by_threshold"] == 0
    assert payload["changes_found"] == 1


@pytest.mark.asyncio
async def test_recent_changes_debounce_treats_equivalent_numeric_states_as_equal() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.humidity": [
                {"state": "40", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "41", "last_changed": "2026-07-21T08:00:10+00:00"},
                {"state": "40.0", "last_changed": "2026-07-21T08:00:20+00:00"},
            ]
        },
        states={"sensor.humidity": _state("40.0", "Humidity", "%")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.humidity"],
        recent_changes={"debounce_seconds": 30},
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["changes"] == []
    assert payload["discarded_by_debounce"] == 2


@pytest.mark.asyncio
async def test_recent_changes_unordered_collections_preserve_duplicate_multiplicity() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.services": [
                {
                    "state": '["pihole", "pihole", "nara-mcp"]',
                    "last_changed": "2026-07-21T08:00:00+00:00",
                },
                {
                    "state": '["nara-mcp", "pihole", "pihole"]',
                    "last_changed": "2026-07-21T08:01:00+00:00",
                },
                {
                    "state": '["nara-mcp", "nara-mcp", "pihole"]',
                    "last_changed": "2026-07-21T08:02:00+00:00",
                },
            ]
        },
        states={"sensor.services": _state("changed", "Services")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.services"],
        recent_changes={"unordered_entities": ["sensor.services"]},
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["discarded_as_reordered_collection"] == 1
    assert payload["changes_found"] == 1
    assert payload["changes"][0]["new_value"] == '["nara-mcp", "nara-mcp", "pihole"]'


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", [None, ""])
async def test_recent_changes_empty_unit_uses_default_threshold(unit: str | None) -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.unitless": [
                {"state": "10", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "10.5", "last_changed": "2026-07-21T08:01:00+00:00"},
            ]
        },
        states={"sensor.unitless": _state("10.5", "Unitless", unit)},
    )
    mcp = _build_mcp(
        client,
        ["sensor.unitless"],
        recent_changes={"numeric_delta_default": 1},
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["discarded_by_threshold"] == 1
    assert payload["changes"] == []


@pytest.mark.asyncio
async def test_recent_changes_debounce_handles_newest_first_input() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.humidity": [
                {"state": "43", "last_changed": "2026-07-21T08:00:30+00:00"},
                {"state": "42", "last_changed": "2026-07-21T08:00:20+00:00"},
                {"state": "41", "last_changed": "2026-07-21T08:00:10+00:00"},
                {"state": "40", "last_changed": "2026-07-21T08:00:00+00:00"},
            ]
        },
        states={"sensor.humidity": _state("43", "Humidity", "%")},
    )
    mcp = _build_mcp(
        client,
        ["sensor.humidity"],
        recent_changes={"debounce_seconds": 30},
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))

    assert payload["changes_found"] == 1
    assert payload["changes"][0]["old_value"] == "40"
    assert payload["changes"][0]["new_value"] == "43"


@pytest.mark.asyncio
async def test_recent_changes_filter_configuration_cannot_expand_allowlist() -> None:
    client = FakeRecentChangesClient(
        histories={
            "sensor.allowed": [
                {"state": "1", "last_changed": "2026-07-21T08:00:00+00:00"},
                {"state": "2", "last_changed": "2026-07-21T08:01:00+00:00"},
            ],
            "sensor.forbidden": [
                {"state": "private", "last_changed": "2026-07-21T08:01:00+00:00"},
            ],
        },
        states={
            "sensor.allowed": _state("2", "Allowed"),
            "sensor.forbidden": _state("private", "Forbidden"),
        },
    )
    mcp = _build_mcp(
        client,
        ["sensor.allowed"],
        recent_changes={
            "numeric_delta_by_entity": {"sensor.forbidden": 1},
            "unordered_entities": ["sensor.forbidden"],
        },
    )

    payload = _payload(await mcp.call_tool("ha_get_recent_changes", {}))
    serialized = json.dumps(payload)

    assert client.requested_entities == ["sensor.allowed"]
    assert client.requested_history_entities == ["sensor.allowed"]
    assert "sensor.forbidden" not in serialized
