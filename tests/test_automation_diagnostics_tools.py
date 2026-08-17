from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from app.config import EntitiesConfig
from app.configuration import Settings
from app.main import load_runtime_entities
from app.repositories.automations import AutomationDiagnosticsRepository
from app.services.automation_diagnostics import (
    AutomationDiagnosticsError,
    AutomationDiagnosticsService,
)
from app.tools import register_tools


RAW_ENTITY = "automation.private_evening_routine"
RAW_AUTOMATION_ID = "18446744073709551615"
RAW_RUN_ID = "012345abcdef012345abcdef"


def _payload(result: tuple) -> dict[str, Any]:
    return result[1]


class FakeRestClient:
    def __init__(self, states: list[dict[str, Any]] | None = None) -> None:
        self.states = states or [
            {
                "entity_id": RAW_ENTITY,
                "state": "on",
                "attributes": {
                    "id": RAW_AUTOMATION_ID,
                    "friendly_name": "Private evening routine",
                    "last_triggered": "2026-07-29T20:00:00+00:00",
                },
            },
            {"entity_id": "sensor.private_room", "state": "21", "attributes": {}},
        ]

    async def list_states(self) -> list[dict[str, Any]]:
        return self.states

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        return next(state for state in self.states if state["entity_id"] == entity_id)

    async def list_services(self) -> list[dict[str, Any]]:
        return [
            {"domain": "notify", "services": {"mobile_app_private": {}}},
            {"domain": "light", "services": {"turn_on": {}}},
            {"domain": "telegram_bot", "services": {"send_message": {}}},
        ]


class FakeWebSocketClient:
    def __init__(self, traces: list[dict[str, Any]] | None = None) -> None:
        self.connected = 0
        self.config_calls: list[str] = []
        self.list_calls: list[str] = []
        self.get_calls: list[tuple[str, str]] = []
        self.traces = traces if traces is not None else [_trace_summary()]

    async def connect(self) -> None:
        self.connected += 1

    async def get_automation_config(self, entity_id: str) -> dict[str, Any]:
        self.config_calls.append(entity_id)
        return {
            "id": RAW_AUTOMATION_ID,
            "alias": "Private evening routine",
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.private_room"}],
            "action": [{"service": "notify.mobile_app_private", "data": {"message": "Secret text"}}],
            "url": "https://private.example.invalid/api",
        }

    async def list_entity_registry(self) -> list[dict[str, Any]]:
        return [
            {"entity_id": RAW_ENTITY, "disabled_by": None},
            {"entity_id": "sensor.private_room", "disabled_by": None},
            {"entity_id": "binary_sensor.private_room", "disabled_by": None},
            {"entity_id": "binary_sensor.alternate_room", "disabled_by": None},
            {"entity_id": "binary_sensor.example_contact", "disabled_by": None},
            {"entity_id": "light.example_accent_lamp", "disabled_by": None},
        ]

    async def list_traces(self, automation_id: str) -> list[dict[str, Any]]:
        self.list_calls.append(automation_id)
        return self.traces

    async def get_trace(self, automation_id: str, run_id: str) -> dict[str, Any]:
        self.get_calls.append((automation_id, run_id))
        return {
            **_trace_summary(),
            "trace": {
                "trigger/0": [{"result": {"platform": "state"}}],
                "condition/0": [{"result": {"result": False}}],
                "action/0/choose/0": [{"result": {"choice": 0}}],
            },
            "context": {"id": "abcdefabcdefabcdefabcdefabcdefab", "user_id": None},
            "error": "Private failure detail",
        }


def _trace_summary() -> dict[str, Any]:
    return {
        "run_id": RAW_RUN_ID,
        "state": "stopped",
        "timestamp": {
            "start": "2026-07-29T20:00:00+00:00",
            "finish": "2026-07-29T20:00:01+00:00",
        },
    }


def _build_mcp(
    *,
    traces: list[dict[str, Any]] | None = None,
    editable: bool = True,
) -> tuple[FastMCP, FakeRestClient, FakeWebSocketClient]:
    mcp = FastMCP(name="Test Nara Home", stateless_http=True, json_response=True)
    rest = FakeRestClient()
    websocket = FakeWebSocketClient(traces)
    register_tools(
        mcp,
        rest,
        EntitiesConfig(editable_automations=[RAW_ENTITY] if editable else []),
        automation_websocket=websocket,
    )
    return mcp, rest, websocket


async def _references(mcp: FastMCP) -> tuple[str, str]:
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    traces = _payload(
        await mcp.call_tool(
            "ha_list_automation_traces",
            {"automation_ref": automation_ref},
        )
    )
    return automation_ref, traces["traces"][0]["run_ref"]


@pytest.mark.asyncio
async def test_v3_tools_are_registered_only_when_websocket_is_injected() -> None:
    mcp, _, _ = _build_mcp()
    names = {tool.name for tool in await mcp.list_tools()}
    assert {
        "ha_list_automations",
        "ha_get_automation_config",
        "ha_list_automation_traces",
        "ha_get_automation_trace",
        "ha_diagnose_automation_trace",
        "ha_get_automation_yaml",
        "ha_search_automations",
        "ha_find_entity_usage",
        "ha_list_automations_detailed",
        "ha_scan_entity_health",
        "ha_find_broken_automation_references",
        "ha_analyze_automation",
        "ha_prepare_automation_edit",
        "ha_get_automation_edit_proposal",
    } <= names
    assert len(names) == 31

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    trace_schema = tools["ha_list_automation_traces"].inputSchema["properties"]
    assert trace_schema["automation_ref"]["pattern"] == "^automation_[0-9]{3,}$"
    assert trace_schema["max_traces"]["minimum"] == 1
    assert trace_schema["max_traces"]["maximum"] == 20
    get_schema = tools["ha_get_automation_trace"].inputSchema
    assert get_schema["required"] == ["automation_ref", "run_ref"]
    assert get_schema["properties"]["run_ref"]["pattern"] == "^run_[0-9]{3,}$"
    proposal_schema = tools["ha_prepare_automation_edit"].inputSchema
    assert proposal_schema["properties"]["replacements"]["minItems"] == 1
    assert proposal_schema["properties"]["replacements"]["maxItems"] == 10


@pytest.mark.asyncio
async def test_list_automations_preserves_authorized_identity_and_anonymizes_sensitive_metadata() -> None:
    mcp, _, websocket = _build_mcp()
    payload = _payload(await mcp.call_tool("ha_list_automations", {}))
    serialized = json.dumps(payload)
    assert payload["automations"][0]["automation_ref"] == "automation_001"
    assert payload["automations"][0]["entity_id"] == RAW_ENTITY
    assert payload["automations"][0]["friendly_name"] == "Private evening routine"
    assert websocket.connected == 0


@pytest.mark.asyncio
async def test_get_config_is_anonymized_and_uses_closed_websocket_method() -> None:
    mcp, _, websocket = _build_mcp()
    automation_ref, _ = await _references(mcp)
    payload = _payload(
        await mcp.call_tool(
            "ha_get_automation_config", {"automation_ref": automation_ref}
        )
    )
    serialized = json.dumps(payload)
    assert websocket.config_calls[-1] == RAW_ENTITY
    assert payload["configuration"]["alias"] == "Private evening routine"
    assert payload["configuration"]["trigger"][0]["entity_id"] == "binary_sensor.private_room"
    assert payload["configuration"]["action"][0]["service"] == "notify.mobile_app_private"
    for secret in (RAW_AUTOMATION_ID, "private.example.invalid", "Secret text"):
        assert secret not in serialized
    assert payload["limits"]["max_total_bytes"] == 512_000


@pytest.mark.asyncio
async def test_trace_references_are_opaque_and_trace_is_anonymized() -> None:
    mcp, _, websocket = _build_mcp()
    automation_ref, run_ref = await _references(mcp)
    payload = _payload(
        await mcp.call_tool(
            "ha_get_automation_trace",
            {"automation_ref": automation_ref, "run_ref": run_ref},
        )
    )
    serialized = json.dumps(payload)
    assert websocket.get_calls == [(RAW_AUTOMATION_ID, RAW_RUN_ID)]
    assert payload["trace"]["run_ref"] == "run_001"
    assert RAW_RUN_ID not in serialized
    assert RAW_AUTOMATION_ID not in serialized
    assert "Private failure detail" not in serialized


@pytest.mark.asyncio
async def test_diagnosis_has_deterministic_evidence_and_human_layers() -> None:
    mcp, _, _ = _build_mcp()
    automation_ref, run_ref = await _references(mcp)
    payload = _payload(
        await mcp.call_tool(
            "ha_diagnose_automation_trace",
            {"automation_ref": automation_ref, "run_ref": run_ref},
        )
    )
    assert payload["evidence"]["duration_seconds"] == 1.0
    assert payload["evidence"]["step_count"] == 3
    assert payload["evidence"]["condition_results"]["false"] == 1
    assert payload["evidence"]["branch_paths_evaluated"] == 1
    assert payload["finding"]
    assert payload["suggestion"]
    assert payload["human_summary"]
    assert 0 <= payload["confidence"] <= 1


@pytest.mark.asyncio
async def test_no_traces_and_invalid_limits_return_clear_tool_errors() -> None:
    mcp, _, _ = _build_mcp(traces=[])
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    with pytest.raises(ToolError, match="No traces exist"):
        await mcp.call_tool(
            "ha_list_automation_traces", {"automation_ref": automation_ref}
        )
    with pytest.raises(ToolError, match="less than or equal to 20"):
        await mcp.call_tool(
            "ha_list_automation_traces",
            {"automation_ref": automation_ref, "max_traces": 21},
        )


@pytest.mark.asyncio
async def test_unknown_references_return_guidance_without_raw_identifiers() -> None:
    mcp, _, _ = _build_mcp()
    with pytest.raises(ToolError, match="call ha_list_automations first"):
        await mcp.call_tool(
            "ha_get_automation_config", {"automation_ref": "automation_999"}
        )

    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    with pytest.raises(ToolError, match="call ha_list_automation_traces first"):
        await mcp.call_tool(
            "ha_get_automation_trace",
            {"automation_ref": automation_ref, "run_ref": "run_999"},
        )


async def _secret_config(entity_id: str) -> dict[str, Any]:
    return {
        "id": RAW_AUTOMATION_ID,
        "alias": "Private evening routine",
        "trigger": [{
            "id": "door_opened",
            "platform": "state",
            "entity_id": "binary_sensor.private_room",
            "value_template": "{{ states('binary_sensor.example_contact') }} token=top-secret-token",
        }],
        "action": [
            {
                "service": "telegram_bot.send_message",
                "target": {"entity_id": "light.example_accent_lamp"},
                "data": {
                    "chat_id": 12345,
                "message": "token=top-secret-token",
                    "webhook_id": "private-hook",
                },
            },
            {"action": "notify.mobile_app_example_device"},
        ],
        "mode": "single",
    }


@pytest.mark.asyncio
async def test_get_automation_yaml_preserves_structure_and_redacts_secrets() -> None:
    mcp, _, websocket = _build_mcp()
    websocket.get_automation_config = _secret_config  # type: ignore[method-assign]
    payload = _payload(
        await mcp.call_tool("ha_get_automation_yaml", {"entity_id": RAW_ENTITY})
    )
    serialized = json.dumps(payload)
    assert payload["automation_ref"] == "automation_001"
    assert payload["entity_id"] == RAW_ENTITY
    assert payload["config"]["trigger"][0]["entity_id"] == "binary_sensor.private_room"
    assert payload["config"]["trigger"][0]["id"] == "door_opened"
    assert payload["config"]["id"] == "[REDACTED:credential]"
    assert "binary_sensor.example_contact" in payload["config"]["trigger"][0]["value_template"]
    assert payload["config"]["action"][0]["service"] == "telegram_bot.send_message"
    assert "trigger:" in payload["yaml"]
    assert payload["redactions"]["chat_id"] == 1
    for secret in (RAW_AUTOMATION_ID, "12345", "private-hook", "top-secret-token"):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_read_payload_keeps_technical_identifiers_but_rejects_headers_and_credentials() -> None:
    mcp, _, websocket = _build_mcp()

    async def sensitive_config(_: str) -> dict[str, Any]:
        return {
            "id": RAW_AUTOMATION_ID,
            "alias": "Exact automation name",
            "trigger": [{"platform": "state", "entity_id": "sensor.exact_technical_id"}],
            "action": [
                {
                    "service": "rest_command.exact_service",
                    "target": {"entity_id": "switch.exact_target"},
                    "headers": {"Authorization": "Bearer private-token"},
                    "data": {"api_key": "private-api-key"},
                }
            ],
        }

    websocket.get_automation_config = sensitive_config  # type: ignore[method-assign]
    payload = _payload(await mcp.call_tool("ha_get_automation_yaml", {"entity_id": RAW_ENTITY}))
    serialized = json.dumps(payload)
    assert payload["alias"] == "Exact automation name"
    assert payload["config"]["trigger"][0]["entity_id"] == "sensor.exact_technical_id"
    assert payload["config"]["action"][0]["service"] == "rest_command.exact_service"
    assert payload["config"]["action"][0]["target"]["entity_id"] == "switch.exact_target"
    for secret in ("Bearer private-token", "private-token", "private-api-key"):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_search_and_usage_return_canonical_exact_paths() -> None:
    mcp, _, websocket = _build_mcp()
    websocket.get_automation_config = _secret_config  # type: ignore[method-assign]
    searched = _payload(
        await mcp.call_tool("ha_search_automations", {"query": "light.example_accent_lamp"})
    )
    assert searched["results"][0]["matches"][0]["path"] == "actions[0].target.entity_id"
    service = _payload(
        await mcp.call_tool("ha_find_entity_usage", {"query": "notify.mobile_app_example_device"})
    )
    assert service["results"][0]["usages"][0]["path"] == "actions[1].action"
    assert service["results"][0]["usages"][0]["section"] == "actions"


@pytest.mark.asyncio
async def test_detailed_list_uses_cached_inventory() -> None:
    mcp, _, websocket = _build_mcp()
    websocket.get_automation_config = _secret_config  # type: ignore[method-assign]
    first = _payload(await mcp.call_tool("ha_list_automations_detailed", {}))
    second = _payload(await mcp.call_tool("ha_list_automations_detailed", {}))
    assert first == second
    assert websocket.connected == 1
    assert first["automations"][0]["mode"] == "single"
    assert first["automations"][0]["triggers"][0]["entity_id"] == "binary_sensor.private_room"


@pytest.mark.asyncio
async def test_yaml_selector_requires_exactly_one_input() -> None:
    mcp, _, _ = _build_mcp()
    with pytest.raises(ToolError, match="exactly one"):
        await mcp.call_tool("ha_get_automation_yaml", {})
    with pytest.raises(ToolError, match="exactly one"):
        await mcp.call_tool(
            "ha_get_automation_yaml",
            {"automation_ref": "automation_001", "entity_id": RAW_ENTITY},
        )


class PartialWebSocketClient(FakeWebSocketClient):
    def __init__(self, failing: set[str]) -> None:
        super().__init__()
        self.failing = failing

    async def get_automation_config(self, entity_id: str) -> dict[str, Any]:
        self.config_calls.append(entity_id)
        if entity_id in self.failing:
            raise RuntimeError(f"internal secret response for {entity_id}")
        return {
            "id": f"internal-{entity_id}",
            "alias": f"Alias {entity_id}",
            "trigger": [],
            "action": [],
            "mode": "single",
        }


def _partial_service(failing_count: int) -> tuple[AutomationDiagnosticsService, PartialWebSocketClient]:
    automation_ids = [f"automation.sample_{index}" for index in range(1, 4)]
    states = [
        {
            "entity_id": entity_id,
            "state": "on",
            "attributes": {"friendly_name": f"Sample {index}"},
        }
        for index, entity_id in enumerate(automation_ids, 1)
    ]
    websocket = PartialWebSocketClient(set(automation_ids[:failing_count]))
    repository = AutomationDiagnosticsRepository(FakeRestClient(states), websocket)
    return AutomationDiagnosticsService(repository), websocket


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing_count", "successful_count"),
    [(1, 2), (2, 1), (3, 0)],
)
async def test_partial_inventory_isolates_one_multiple_or_all_failures(
    failing_count: int, successful_count: int
) -> None:
    service, _ = _partial_service(failing_count)
    payload = await service.list_automations_detailed(100)
    serialized = json.dumps(payload)
    assert payload["partial"] is True
    assert payload["count"] == successful_count
    assert len(payload["errors"]) == failing_count
    assert "internal secret response" not in serialized
    for index, error in enumerate(payload["errors"], 1):
        assert error == {
            "automation_ref": f"automation_{index:03d}",
            "entity_id": f"automation.sample_{index}",
            "error_code": "config_unavailable",
            "message": "Automation configuration is temporarily unavailable.",
        }


@pytest.mark.asyncio
async def test_partial_inventory_is_cached_without_retrying_failures() -> None:
    service, websocket = _partial_service(1)
    first = await service.list_automations_detailed(100)
    calls_after_first = list(websocket.config_calls)
    second = await service.search_automations("single", 25, 20)
    assert websocket.config_calls == calls_after_first
    assert first["errors"] == second["errors"]
    assert second["partial"] is True


@pytest.mark.asyncio
async def test_partial_inventory_recovers_after_next_rebuild() -> None:
    service, websocket = _partial_service(1)
    failed = await service.list_automations_detailed(100)
    assert failed["partial"] is True
    websocket.failing.clear()
    service.invalidate_inventory()
    recovered = await service.list_automations_detailed(100)
    assert recovered["partial"] is False
    assert recovered["errors"] == []
    assert recovered["count"] == 3
    assert len(websocket.config_calls) == 6


async def _broken_config(_: str) -> dict[str, Any]:
    return {
        "id": RAW_AUTOMATION_ID,
        "alias": "Technical automation name",
        "trigger": [
            {"platform": "state", "entity_id": "binary_sensor.removed_contact"},
            {"platform": "state", "entity_id": "binary_sensor.disabled_contact"},
        ],
        "condition": [
            {"condition": "state", "entity_id": "sensor.unknown_temperature"}
        ],
        "action": [
            {"service": "light.missing_service", "target": {"entity_id": "light.available_lamp"}},
            {"service": "light.turn_on", "target": {"entity_id": "light.unavailable_lamp"}},
            {"value_template": "{{ states('sensor.removed_template_sensor') }} token=top-secret-token"},
        ],
    }


async def _example_config(_: str) -> dict[str, Any]:
    return {
        "id": "example-evening-control",
        "alias": "Example evening control",
        "triggers": [
            {"trigger": "state", "entity_id": "binary_sensor.example_room_presence"},
            {"trigger": "state", "entity_id": "binary_sensor.example_room_presence"},
        ],
        "actions": [
            {
                "choose": [
                    {
                        "conditions": [
                            {
                                "condition": "state",
                                "entity_id": "light.example_accent_lamp",
                            }
                        ],
                        "sequence": [
                            {
                                "action": "light.turn_on",
                                "target": {"entity_id": "light.example_accent_lamp"},
                            }
                        ],
                    },
                    {
                        "sequence": [
                            {
                                "action": "light.turn_off",
                                "target": {"entity_id": "light.example_accent_lamp"},
                            }
                        ]
                    },
                ]
            }
        ],
    }


@pytest.mark.asyncio
async def test_sample_proposal_shape_has_five_occurrences_and_three_exact_light_paths() -> None:
    target = "automation.example_evening_lighting"
    rest = FakeRestClient(
        [
            {"entity_id": target, "state": "on", "attributes": {"id": "example-evening-control"}},
            {
                "entity_id": "binary_sensor.example_room_presence",
                "state": "off",
                "attributes": {},
            },
            {"entity_id": "light.example_accent_lamp", "state": "off", "attributes": {}},
        ]
    )
    websocket = FakeWebSocketClient()
    websocket.get_automation_config = _example_config  # type: ignore[method-assign]
    service = AutomationDiagnosticsService(
        AutomationDiagnosticsRepository(rest, websocket),
        editable_automations={target},
    )
    listed = await service.list_automations()
    analysis = await service.analyze_automation(
        listed["automations"][0]["automation_ref"], 200
    )
    light_occurrences = [
        item
        for item in analysis["editable_occurrences"]
        if item["current_entity_id"] == "light.example_accent_lamp"
    ]

    assert analysis["edit_proposal_eligible"] is True
    assert len(analysis["editable_occurrences"]) == 5
    assert {item["path"] for item in light_occurrences} == {
        "actions[0].choose[0].conditions[0].entity_id",
        "actions[0].choose[0].sequence[0].target.entity_id",
        "actions[0].choose[1].sequence[0].target.entity_id",
    }
    assert all(item["health_status"] == "healthy" for item in light_occurrences)


@pytest.mark.asyncio
async def test_phase4a_finds_structured_broken_references_with_exact_paths_and_ids() -> None:
    mcp, rest, websocket = _build_mcp()
    rest.states.extend(
        [
            {"entity_id": "sensor.unknown_temperature", "state": "unknown", "attributes": {}},
            {"entity_id": "light.available_lamp", "state": "off", "attributes": {}},
            {"entity_id": "light.unavailable_lamp", "state": "unavailable", "attributes": {}},
        ]
    )
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    original_registry = websocket.list_entity_registry

    async def registry() -> list[dict[str, Any]]:
        return [
            *await original_registry(),
            {"entity_id": "binary_sensor.disabled_contact", "disabled_by": "user"},
            {"entity_id": "sensor.unknown_temperature", "disabled_by": None},
            {"entity_id": "light.available_lamp", "disabled_by": None},
            {"entity_id": "light.unavailable_lamp", "disabled_by": None},
        ]

    websocket.list_entity_registry = registry  # type: ignore[method-assign]
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    result = _payload(
        await mcp.call_tool(
            "ha_analyze_automation",
            {"automation_ref": listed["automations"][0]["automation_ref"]},
        )
    )
    by_path = {finding["path"]: finding for finding in result["findings"]}
    assert result["alias"] == "Technical automation name"
    assert by_path["triggers[0].entity_id"]["entity_id"] == "binary_sensor.removed_contact"
    assert by_path["triggers[0].entity_id"]["status"] == "missing"
    assert by_path["triggers[1].entity_id"]["status"] == "disabled"
    assert by_path["conditions[0].entity_id"]["status"] == "unknown"
    assert by_path["actions[0].service"]["service"] == "light.missing_service"
    assert by_path["actions[0].service"]["status"] == "service_missing"
    assert by_path["actions[1].target.entity_id"]["status"] == "unavailable"
    template = by_path["actions[2].value_template"]
    assert template["entity_id"] == "sensor.removed_template_sensor"
    assert template["status"] == "possible_template_reference"
    assert template["referenced_status"] == "missing"
    editable_findings = [finding for finding in result["findings"] if finding["kind"] == "entity"]
    assert all(finding["occurrence_ref"].startswith("occ_") for finding in editable_findings)
    assert "occurrence_ref" not in by_path["actions[0].service"]
    assert "occurrence_ref" not in template
    assert "top-secret-token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_phase4a_health_scan_preserves_names_and_sanitizes_catalog_errors() -> None:
    mcp, rest, websocket = _build_mcp()
    rest.states.append(
        {
            "entity_id": "sensor.unavailable_probe",
            "state": "unavailable",
            "attributes": {"friendly_name": "Exact technical probe name"},
        }
    )
    result = _payload(await mcp.call_tool("ha_scan_entity_health", {}))
    probe = next(item for item in result["entities"] if item["entity_id"] == "sensor.unavailable_probe")
    assert probe == {
        "entity_id": "sensor.unavailable_probe",
        "friendly_name": "Exact technical probe name",
        "status": "unavailable",
    }
    assert result["partial"] is False


@pytest.mark.asyncio
async def test_phase4a_partial_catalog_does_not_claim_absent_entities_are_missing() -> None:
    service, websocket = _partial_service(0)

    async def failed_registry() -> list[dict[str, Any]]:
        raise RuntimeError("token=registry-secret")

    websocket.list_entity_registry = failed_registry  # type: ignore[attr-defined]
    payload = await service.analyze_automation("automation_001", 200)
    assert payload["partial"] is True
    assert "registry-secret" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_phase4a_analysis_rate_limit_is_bounded_in_memory() -> None:
    service, _ = _partial_service(0)
    for _ in range(30):
        await service.scan_entity_health(200)
    with pytest.raises(AutomationDiagnosticsError, match="rate limit exceeded"):
        await service.scan_entity_health(200)


@pytest.mark.asyncio
async def test_phase4b_prepares_and_reads_exact_diff_without_writing() -> None:
    mcp, _, websocket = _build_mcp()

    async def tracked_config(entity_id: str) -> dict[str, Any]:
        websocket.config_calls.append(entity_id)
        return await _broken_config(entity_id)

    websocket.get_automation_config = tracked_config  # type: ignore[method-assign]
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    analysis = _payload(
        await mcp.call_tool(
            "ha_analyze_automation",
            {"automation_ref": automation_ref},
        )
    )
    occurrence = next(
        finding
        for finding in analysis["findings"]
        if finding.get("entity_id") == "binary_sensor.removed_contact"
    )
    proposal = _payload(
        await mcp.call_tool(
            "ha_prepare_automation_edit",
            {
                "automation_ref": automation_ref,
                "replacements": [
                    {
                        "occurrence_ref": occurrence["occurrence_ref"],
                        "replacement_entity_id": "binary_sensor.private_room",
                    }
                ],
            },
        )
    )
    assert proposal["write_capability"] is False
    assert proposal["replacement_count"] == 1
    assert proposal["diff"] == [
        {
            "occurrence_ref": occurrence["occurrence_ref"],
            "path": "triggers[0].entity_id",
            "before_entity_id": "binary_sensor.removed_contact",
            "after_entity_id": "binary_sensor.private_room",
        }
    ]
    assert proposal["base_digest"].startswith("sha256:")
    assert proposal["candidate_digest"].startswith("sha256:")
    assert proposal["candidate_digest"] != proposal["base_digest"]
    assert proposal["confirmation"]["required_for_future_apply"] is True
    assert "top-secret-token" not in json.dumps(proposal)

    retrieved = _payload(
        await mcp.call_tool(
            "ha_get_automation_edit_proposal",
            {"proposal_ref": proposal["proposal_ref"]},
        )
    )
    assert retrieved == proposal
    # The third read revalidates the exact proposal source before returning it.
    assert websocket.config_calls.count(RAW_ENTITY) == 3


@pytest.mark.asyncio
async def test_phase4b_emits_healthy_structured_occurrences_for_allowlisted_automation() -> None:
    mcp, _, _ = _build_mcp()
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    analysis_result = await mcp.call_tool(
        "ha_analyze_automation", {"automation_ref": automation_ref}
    )
    analysis = _payload(analysis_result)
    occurrence = analysis["editable_occurrences"][0]
    assert occurrence["occurrence_ref"].startswith("occ_")
    assert occurrence["path"] == "triggers[0].entity_id"
    assert occurrence["current_entity_id"] == "binary_sensor.private_room"
    assert occurrence["health_status"] == "healthy"
    assert occurrence["automation_ref"] == automation_ref
    assert occurrence["automation_entity_id"] == RAW_ENTITY
    assert occurrence["inventory_generation"] == 1
    assert analysis["edit_proposal_eligible"] is True
    assert '"edit_proposal_eligible": true' in analysis_result[0][0].text
    proposal = _payload(
        await mcp.call_tool(
            "ha_prepare_automation_edit",
            {
                "automation_ref": automation_ref,
                "replacements": [
                    {
                        "occurrence_ref": occurrence["occurrence_ref"],
                        "replacement_entity_id": "binary_sensor.alternate_room",
                    }
                ],
            },
        )
    )
    assert proposal["write_capability"] is False
    assert proposal["diff"][0]["before_entity_id"] == "binary_sensor.private_room"
    assert proposal["diff"][0]["after_entity_id"] == (
        "binary_sensor.alternate_room"
    )


@pytest.mark.asyncio
async def test_phase4b_full_settings_file_load_registration_and_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "configured"
    config_dir.mkdir()
    entities_path = config_dir / "entities.yaml"
    entities_path.write_text(
        "editable_automations:\n"
        f"  - {RAW_ENTITY}\n",
        encoding="utf-8",
    )
    env_path = tmp_path / "service.env"
    env_path.write_text(
        "HA_URL=http://ha.example.invalid:8123\n"
        "HA_TOKEN=integration-token\n"
        "ENTITIES_FILE=configured/entities.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENTITIES_FILE", raising=False)
    settings = Settings(_env_file=env_path)
    entities = load_runtime_entities(settings)

    mcp = FastMCP(name="Integration", stateless_http=True, json_response=True)
    rest = FakeRestClient()
    websocket = FakeWebSocketClient()
    register_tools(mcp, rest, entities, automation_websocket=websocket)
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    analysis = _payload(
        await mcp.call_tool("ha_analyze_automation", {"automation_ref": automation_ref})
    )

    assert settings.entities_file == Path("configured/entities.yaml")
    assert entities.editable_automations == [RAW_ENTITY]
    assert analysis["edit_proposal_eligible"] is True
    assert len(analysis["editable_occurrences"]) == 1


@pytest.mark.asyncio
async def test_phase4b_rejects_non_allowlisted_automation() -> None:
    mcp, _, websocket = _build_mcp(editable=False)
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    analysis = _payload(
        await mcp.call_tool("ha_analyze_automation", {"automation_ref": automation_ref})
    )
    assert analysis["edit_proposal_eligible"] is False
    assert analysis["editable_occurrences"] == []
    assert all("occurrence_ref" not in finding for finding in analysis["findings"])
    with pytest.raises(ToolError, match="not allowlisted"):
        await mcp.call_tool(
            "ha_prepare_automation_edit",
            {
                "automation_ref": automation_ref,
                "replacements": [
                    {
                        "occurrence_ref": "occ_0000000000000000",
                        "replacement_entity_id": "binary_sensor.private_room",
                    }
                ],
            },
        )


@pytest.mark.asyncio
async def test_phase4b_rejects_template_occurrences_and_arbitrary_paths() -> None:
    mcp, _, websocket = _build_mcp()
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    analysis = _payload(
        await mcp.call_tool("ha_analyze_automation", {"automation_ref": automation_ref})
    )
    template = next(finding for finding in analysis["findings"] if finding["kind"] == "template")
    assert "occurrence_ref" not in template
    assert all(item["kind"] == "entity" for item in analysis["editable_occurrences"])
    with pytest.raises(ToolError, match="Unknown occurrence reference"):
        await mcp.call_tool(
            "ha_prepare_automation_edit",
            {
                "automation_ref": automation_ref,
                "replacements": [
                    {
                        "occurrence_ref": "occ_0000000000000000",
                        "replacement_entity_id": "sensor.private_room",
                    }
                ],
            },
        )
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "ha_prepare_automation_edit",
            {
                "automation_ref": automation_ref,
                "replacements": [
                    {
                        "occurrence_ref": "occ_0000000000000000",
                        "replacement_entity_id": "sensor.private_room",
                        "path": "actions[0]",
                        "yaml": "action: malicious",
                    }
                ],
            },
        )


@pytest.mark.asyncio
async def test_phase4b_occurrence_is_scoped_to_originating_automation() -> None:
    states = [
        {
            "entity_id": "automation.first",
            "state": "on",
            "attributes": {"id": "1", "friendly_name": "First"},
        },
        {
            "entity_id": "automation.second",
            "state": "on",
            "attributes": {"id": "2", "friendly_name": "Second"},
        },
        {"entity_id": "binary_sensor.private_room", "state": "off", "attributes": {}},
    ]
    rest = FakeRestClient(states)
    websocket = FakeWebSocketClient()
    service = AutomationDiagnosticsService(
        AutomationDiagnosticsRepository(rest, websocket),
        editable_automations={"automation.first", "automation.second"},
    )
    listed = await service.list_automations()
    first_ref = listed["automations"][0]["automation_ref"]
    second_ref = listed["automations"][1]["automation_ref"]
    analysis = await service.analyze_automation(first_ref, 200)
    occurrence_ref = analysis["editable_occurrences"][0]["occurrence_ref"]
    with pytest.raises(AutomationDiagnosticsError, match="Unknown occurrence reference"):
        await service.prepare_automation_edit(
            second_ref,
            [{"occurrence_ref": occurrence_ref, "replacement_entity_id": "binary_sensor.private_room"}],
        )


@pytest.mark.asyncio
async def test_phase4b_occurrence_expires_when_inventory_is_rebuilt() -> None:
    rest = FakeRestClient()
    websocket = FakeWebSocketClient()
    service = AutomationDiagnosticsService(
        AutomationDiagnosticsRepository(rest, websocket),
        editable_automations={RAW_ENTITY},
    )
    listed = await service.list_automations()
    automation_ref = listed["automations"][0]["automation_ref"]
    first = await service.analyze_automation(automation_ref, 200)
    occurrence_ref = first["editable_occurrences"][0]["occurrence_ref"]
    service.invalidate_inventory()
    second = await service.analyze_automation(automation_ref, 200)
    assert second["editable_occurrences"][0]["occurrence_ref"] != occurrence_ref
    assert second["editable_occurrences"][0]["inventory_generation"] == 2
    with pytest.raises(AutomationDiagnosticsError, match="Unknown occurrence reference"):
        await service.prepare_automation_edit(
            automation_ref,
            [{"occurrence_ref": occurrence_ref, "replacement_entity_id": "binary_sensor.private_room"}],
        )


@pytest.mark.asyncio
async def test_phase4b_rejects_stale_configuration_before_creating_proposal() -> None:
    mcp, _, websocket = _build_mcp()
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    analysis = _payload(
        await mcp.call_tool("ha_analyze_automation", {"automation_ref": automation_ref})
    )
    occurrence = next(finding for finding in analysis["findings"] if finding["kind"] == "entity")

    async def changed_config(entity_id: str) -> dict[str, Any]:
        config = await _broken_config(entity_id)
        config["mode"] = "restart"
        return config

    websocket.get_automation_config = changed_config  # type: ignore[method-assign]
    with pytest.raises(ToolError, match="configuration changed"):
        await mcp.call_tool(
            "ha_prepare_automation_edit",
            {
                "automation_ref": automation_ref,
                "replacements": [
                    {
                        "occurrence_ref": occurrence["occurrence_ref"],
                        "replacement_entity_id": "binary_sensor.private_room",
                    }
                ],
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement_entity_id", "error"),
    [
        ("sensor.private_room", "domain must match"),
        ("binary_sensor.entity_that_does_not_exist", "must exist and be healthy"),
    ],
)
async def test_phase4b_requires_healthy_domain_compatible_replacement(
    replacement_entity_id: str,
    error: str,
) -> None:
    mcp, _, websocket = _build_mcp()
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    listed = _payload(await mcp.call_tool("ha_list_automations", {}))
    automation_ref = listed["automations"][0]["automation_ref"]
    analysis = _payload(
        await mcp.call_tool("ha_analyze_automation", {"automation_ref": automation_ref})
    )
    occurrence = next(
        finding
        for finding in analysis["findings"]
        if finding.get("entity_id") == "binary_sensor.removed_contact"
    )
    with pytest.raises(ToolError, match=error):
        await mcp.call_tool(
            "ha_prepare_automation_edit",
            {
                "automation_ref": automation_ref,
                "replacements": [
                    {
                        "occurrence_ref": occurrence["occurrence_ref"],
                        "replacement_entity_id": replacement_entity_id,
                    }
                ],
            },
        )


@pytest.mark.asyncio
async def test_phase4b_expired_proposal_cannot_be_read() -> None:
    rest = FakeRestClient()
    websocket = FakeWebSocketClient()
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    service = AutomationDiagnosticsService(
        AutomationDiagnosticsRepository(rest, websocket),
        editable_automations={RAW_ENTITY},
    )
    listed = await service.list_automations()
    automation_ref = listed["automations"][0]["automation_ref"]
    analysis = await service.analyze_automation(automation_ref, 200)
    occurrence = next(finding for finding in analysis["findings"] if finding["kind"] == "entity")
    proposal = await service.prepare_automation_edit(
        automation_ref,
        [
            {
                "occurrence_ref": occurrence["occurrence_ref"],
                "replacement_entity_id": "binary_sensor.private_room",
            }
        ],
    )
    service._proposals[proposal["proposal_ref"]]["expires_monotonic"] = 0
    with pytest.raises(AutomationDiagnosticsError, match="Unknown or expired"):
        await service.get_automation_edit_proposal(proposal["proposal_ref"])


@pytest.mark.asyncio
async def test_phase4b_proposal_survives_irrelevant_inventory_change() -> None:
    rest = FakeRestClient()
    websocket = FakeWebSocketClient()
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    service = AutomationDiagnosticsService(
        AutomationDiagnosticsRepository(rest, websocket), editable_automations={RAW_ENTITY}
    )
    automation_ref = (await service.list_automations())["automations"][0]["automation_ref"]
    analysis = await service.analyze_automation(automation_ref, 200)
    occurrence = next(
        finding for finding in analysis["findings"]
        if finding.get("entity_id") == "binary_sensor.removed_contact"
    )
    proposal = await service.prepare_automation_edit(
        automation_ref,
        [{"occurrence_ref": occurrence["occurrence_ref"], "replacement_entity_id": "binary_sensor.private_room"}],
    )
    rest.states.append({"entity_id": "sensor.unrelated_new", "state": "1", "attributes": {}})
    assert await service.get_automation_edit_proposal(proposal["proposal_ref"]) == proposal


@pytest.mark.asyncio
async def test_phase4b_proposal_is_invalidated_when_source_changes_after_creation() -> None:
    rest = FakeRestClient()
    websocket = FakeWebSocketClient()
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    service = AutomationDiagnosticsService(
        AutomationDiagnosticsRepository(rest, websocket), editable_automations={RAW_ENTITY}
    )
    automation_ref = (await service.list_automations())["automations"][0]["automation_ref"]
    analysis = await service.analyze_automation(automation_ref, 200)
    occurrence = next(
        finding for finding in analysis["findings"]
        if finding.get("entity_id") == "binary_sensor.removed_contact"
    )
    proposal = await service.prepare_automation_edit(
        automation_ref,
        [{"occurrence_ref": occurrence["occurrence_ref"], "replacement_entity_id": "binary_sensor.private_room"}],
    )

    async def changed(entity_id: str) -> dict[str, Any]:
        config = await _broken_config(entity_id)
        config["mode"] = "restart"
        return config

    websocket.get_automation_config = changed  # type: ignore[method-assign]
    with pytest.raises(AutomationDiagnosticsError, match="configuration changed"):
        await service.get_automation_edit_proposal(proposal["proposal_ref"])


@pytest.mark.asyncio
async def test_phase4b_proposal_is_invalidated_when_destination_changes() -> None:
    rest = FakeRestClient()
    websocket = FakeWebSocketClient()
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    service = AutomationDiagnosticsService(
        AutomationDiagnosticsRepository(rest, websocket), editable_automations={RAW_ENTITY}
    )
    automation_ref = (await service.list_automations())["automations"][0]["automation_ref"]
    analysis = await service.analyze_automation(automation_ref, 200)
    occurrence = next(
        finding for finding in analysis["findings"]
        if finding.get("entity_id") == "binary_sensor.removed_contact"
    )
    proposal = await service.prepare_automation_edit(
        automation_ref,
        [{"occurrence_ref": occurrence["occurrence_ref"], "replacement_entity_id": "binary_sensor.private_room"}],
    )

    original_list_registry = websocket.list_entity_registry

    async def destination_removed() -> list[dict[str, Any]]:
        return [
            item for item in await original_list_registry()
            if item.get("entity_id") != "binary_sensor.private_room"
        ]

    websocket.list_entity_registry = destination_removed  # type: ignore[method-assign]
    with pytest.raises(AutomationDiagnosticsError, match="destination changed"):
        await service.get_automation_edit_proposal(proposal["proposal_ref"])


@pytest.mark.asyncio
async def test_phase4b_proposal_is_invalidated_when_policy_changes() -> None:
    rest = FakeRestClient()
    websocket = FakeWebSocketClient()
    websocket.get_automation_config = _broken_config  # type: ignore[method-assign]
    service = AutomationDiagnosticsService(
        AutomationDiagnosticsRepository(rest, websocket), editable_automations={RAW_ENTITY}
    )
    automation_ref = (await service.list_automations())["automations"][0]["automation_ref"]
    analysis = await service.analyze_automation(automation_ref, 200)
    occurrence = next(
        finding for finding in analysis["findings"]
        if finding.get("entity_id") == "binary_sensor.removed_contact"
    )
    proposal = await service.prepare_automation_edit(
        automation_ref,
        [{"occurrence_ref": occurrence["occurrence_ref"], "replacement_entity_id": "binary_sensor.private_room"}],
    )

    service._proposal_policy_digest = "sha256:changed"
    with pytest.raises(AutomationDiagnosticsError, match="policy changed"):
        await service.get_automation_edit_proposal(proposal["proposal_ref"])
