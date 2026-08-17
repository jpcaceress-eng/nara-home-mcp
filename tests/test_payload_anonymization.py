from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest

from app.devtools import (
    AnonymizationLimits,
    CaptureAnonymizer,
    CaptureAuditError,
    CaptureOperationError,
    audit_serialized_capture,
    capture_automation_diagnostics,
    list_automations,
)


def serialize(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def test_anonymization_does_not_modify_original_and_preserves_structure() -> None:
    original = {
        "entity_id": "binary_sensor.example_room_presence",
        "attributes": {"state": "on", "values": [1, True, None]},
    }
    untouched = copy.deepcopy(original)

    anonymized = CaptureAnonymizer().anonymize(original)

    assert original == untouched
    assert anonymized == {
        "entity_id": "binary_sensor.entity_001",
        "attributes": {"state": "on", "values": [1, True, None]},
    }
    assert anonymized is not original
    assert anonymized["attributes"] is not original["attributes"]


def test_pseudonyms_are_stable_and_entity_domains_are_preserved() -> None:
    anonymizer = CaptureAnonymizer()
    payload = {
        "first": "binary_sensor.example_room_presence",
        "second": "binary_sensor.example_room_presence",
        "third": "light.lampara_privada",
    }

    result = anonymizer.anonymize(payload)

    assert result["first"] == result["second"] == "binary_sensor.entity_001"
    assert result["third"] == "light.entity_002"
    assert "presencia" not in serialize(result)
    assert "lampara" not in serialize(result)


def test_authorized_automation_identity_is_preserved_recursively() -> None:
    payload = {
        "entity_id": "automation.morning_lights",
        "alias": "Morning lights",
        "nested": {
            "friendly_name": "Morning lights",
            "entity_id": ["automation.morning_lights", "sensor.private_room"],
        },
    }

    result = CaptureAnonymizer().anonymize(payload)

    assert result["entity_id"] == "automation.morning_lights"
    assert result["alias"] == "Morning lights"
    assert result["nested"]["friendly_name"] == "Morning lights"
    assert result["nested"]["entity_id"] == [
        "automation.morning_lights",
        "sensor.entity_001",
    ]


def test_visible_names_still_remove_sensitive_values_and_explicit_people() -> None:
    payload = {
        "name": "Routine owner@example.com via https://internal.example",
        "alias": "Notify owner@example.com",
        "person_name": "Private Person",
        "device_name": "Private phone",
        "nested": {"token": "small-secret", "location": "Private address"},
    }

    result = CaptureAnonymizer().anonymize(payload)
    serialized = serialize(result)

    assert result["name"].startswith("Routine email_001 via url_001")
    assert result["alias"] == "Notify email_001"
    assert result["person_name"] == "[REDACTED:name]"
    assert result["device_name"] == "[REDACTED:name]"
    assert result["nested"]["token"] == "[REDACTED:credential]"
    assert result["nested"]["location"] == "[REDACTED:location]"
    for sensitive in ("owner@example.com", "internal.example", "Private Person", "Private phone", "small-secret", "Private address"):
        assert sensitive not in serialized


def test_all_identifier_categories_are_pseudonymized() -> None:
    payload = {
        "device_id": "real-device-id",
        "area_id": "real-area-id",
        "context": {"id": "real-context-id", "parent_id": "real-parent-id"},
        "user_id": "real-user-id",
        "automation_id": "real-automation-id",
        "item_id": "real-automation-id",
        "run_id": "real-run-id",
        "id": "real-config-id",
    }

    result = CaptureAnonymizer().anonymize(payload)

    assert result["device_id"] == "device_001"
    assert result["area_id"] == "area_001"
    assert result["context"]["id"] == "context_001"
    assert result["context"]["parent_id"] == "context_002"
    assert result["user_id"] == "user_001"
    assert result["automation_id"] == result["item_id"] == "automation_001"
    assert result["run_id"] == "run_001"
    assert result["id"] == "automation_002"
    assert "real-" not in serialize(result)


def test_identifier_lists_preserve_list_shape() -> None:
    result = CaptureAnonymizer().anonymize(
        {
            "entity_id": ["sensor.private_one", "sensor.private_two"],
            "device_id": ["device-one", "device-two"],
        }
    )

    assert result["entity_id"] == ["sensor.entity_001", "sensor.entity_002"]
    assert result["device_id"] == ["device_001", "device_002"]


def test_tokens_credentials_headers_and_value_patterns_are_redacted() -> None:
    payload = {
        "token": "small-secret",
        "headers": {"Authorization": "Bearer highly-private-token"},
        "unknown": "Bearer another-private-token",
        "opaque": "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    }

    result = CaptureAnonymizer().anonymize(payload)
    serialized = serialize(result)

    assert result["token"] == "[REDACTED:credential]"
    assert result["headers"] == "[REDACTED:headers]"
    assert "Bearer " not in serialized
    assert "private-token" not in serialized
    assert "AbCdEf" not in serialized


def test_urls_ips_emails_and_coordinates_are_removed_by_value() -> None:
    payload = {
        "unknown_url": "https://ha.private.example:8123/path",
        "socket": "wss://203.0.113.20/api/websocket",
        "owner": "private.person@example.com",
        "public_ip": "8.8.8.8",
        "ipv6": "2001:db8::1",
        "position": "40.4168, -3.7038",
        "latitude": 40.4168,
        "longitude": -3.7038,
    }

    result = CaptureAnonymizer().anonymize(payload)
    serialized = serialize(result)

    for private_value in (
        "ha.private.example",
        "203.0.113.20",
        "private.person@example.com",
        "8.8.8.8",
        "2001:db8::1",
        "40.4168",
        "-3.7038",
    ):
        assert private_value not in serialized
    audit_serialized_capture({"capture.json": serialized})


def test_people_locations_addresses_and_notifications_are_redacted() -> None:
    payload = {
        "person_name": "Taylor Example",
        "person": "Morgan Example",
        "address": "Example Street 123",
        "location": {"city": "Example City", "place": "Example Home"},
        "notification": {"title": "Alert", "message": "Taylor arrived"},
    }

    result = CaptureAnonymizer().anonymize(payload)
    serialized = serialize(result)

    assert result["location"] == {
        "city": "[REDACTED:location]",
        "place": "[REDACTED:location]",
    }
    assert result["notification"] == {
        "title": "[REDACTED:notification]",
        "message": "[REDACTED:notification]",
    }
    for value in ("Taylor", "Morgan", "Street", "Example City", "Alert", "arrived"):
        assert value not in serialized


def test_nested_mapping_keys_containing_entity_ids_are_anonymized() -> None:
    payload = {
        "trace": {
            "condition/0": [
                {"changed_variables": {"sensor.private_temperature": "on"}}
            ]
        }
    }

    result = CaptureAnonymizer().anonymize(payload)

    changed = result["trace"]["condition/0"][0]["changed_variables"]
    assert changed == {"sensor.entity_001": "on"}


def test_string_depth_collection_and_total_size_limits_are_marked() -> None:
    limits = AnonymizationLimits(
        max_depth=3,
        max_string_length=12,
        max_collection_items=2,
        max_total_bytes=100,
    )
    result = CaptureAnonymizer(limits).anonymize(
        {
            "path": "condition/very/long/path",
            "items": [1, 2, 3, 4],
            "nested": {"one": {"two": {"three": "value"}}},
            "extra": "more data to exceed the configured approximate total",
        }
    )
    serialized = serialize(result)

    assert "[TRUNCATED:" in serialized
    assert len(result) <= 3  # two configured items plus the truncation marker


@pytest.mark.parametrize(
    ("unsafe", "expected_code"),
    [
        ("https://private.example", "url"),
        ("Bearer private-token", "bearer_token"),
        ("owner@private.example", "email"),
        ("203.0.113.10", "ip_address"),
        ("40.4168, -3.7038", "coordinates"),
        ("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "possible_secret"),
    ],
)
def test_final_audit_rejects_sensitive_patterns(unsafe: str, expected_code: str) -> None:
    with pytest.raises(CaptureAuditError) as captured:
        audit_serialized_capture({"capture.json": json.dumps({"value": unsafe})})

    assert expected_code in captured.value.issue_codes
    assert unsafe not in str(captured.value)


def test_final_audit_does_not_treat_adjacent_json_numbers_as_coordinates() -> None:
    audit_serialized_capture(
        {
            "proposal.json": json.dumps(
                {
                    "inventory_generation": 1,
                    "replacement_count": 1,
                    "write_capability": False,
                }
            )
        }
    )


def test_final_audit_still_rejects_explicit_numeric_coordinate_fields() -> None:
    with pytest.raises(CaptureAuditError) as captured:
        audit_serialized_capture(
            {"capture.json": json.dumps({"latitude": 40.4168})}
        )
    assert "coordinates" in captured.value.issue_codes


def test_final_audit_rejects_known_real_identifiers() -> None:
    with pytest.raises(CaptureAuditError) as captured:
        audit_serialized_capture(
            {"capture.json": '{"value":"internal-real-id"}'},
            forbidden_values=["internal-real-id"],
        )

    assert captured.value.issue_codes == ("known_identifier",)


def test_final_audit_distinguishes_context_id_from_automation_id() -> None:
    safe_capture = {
        "entity_id": "automation.entity_001",
        "attributes": {"id": "automation_001"},
        "context": {
            "id": "context_001",
            "parent_id": "context_002",
            "user_id": "user_001",
        },
        "trace": {
            "action/0": [
                {"changed_variables": {"context": {"id": "context_003"}}}
            ],
            "trigger": [
                {
                    "changed_variables": {
                        "this": {"context": {"id": "context_004"}}
                    }
                }
            ],
        },
    }

    audit_serialized_capture({"state.json": serialize(safe_capture)})

    unsafe_capture = copy.deepcopy(safe_capture)
    unsafe_capture["context"]["id"] = "real-context-id"
    with pytest.raises(CaptureAuditError) as captured:
        audit_serialized_capture({"state.json": serialize(unsafe_capture)})

    assert captured.value.issue_codes == ("raw_context_id",)


def test_final_audit_allows_long_authorized_automation_entity_id() -> None:
    audit_serialized_capture(
        {
            "response.json": serialize(
                {
                    "entity_id": "automation.example_motion_lighting",
                    "friendly_name": "Luz suave del salón",
                }
            )
        }
    )


def test_final_audit_allows_long_entity_id_only_for_phase1_contract() -> None:
    document = {
        "query": "binary_sensor.example_contact_with_a_deliberately_long_identifier"
    }
    with pytest.raises(CaptureAuditError):
        audit_serialized_capture({"response.json": serialize(document)})
    audit_serialized_capture(
        {"response.json": serialize(document)}, allow_raw_entity_ids=True
    )


class FakeRestClient:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.calls: list[str] = []
        self.closed = False

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        self.calls.append(entity_id)
        return copy.deepcopy(self.state)

    async def list_states(self) -> list[dict[str, Any]]:
        self.calls.append("list_states")
        return copy.deepcopy(self.state) if isinstance(self.state, list) else [copy.deepcopy(self.state)]

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_list_automations_uses_only_rest_and_prints_expected_columns() -> None:
    rest = FakeRestClient(
        [
            {
                "entity_id": "sensor.temperature",
                "state": "20",
                "attributes": {"friendly_name": "Temperature"},
            },
            {
                "entity_id": "automation.second",
                "state": "off",
                "attributes": {"friendly_name": "Second", "last_triggered": None},
            },
            {
                "entity_id": "automation.first",
                "state": "on",
                "attributes": {
                    "friendly_name": "First\nAutomation",
                    "last_triggered": "2026-07-29T10:00:00+00:00",
                },
            },
        ]
    )
    terminal = io.StringIO()

    count = await list_automations(rest, output=terminal)

    lines = terminal.getvalue().splitlines()
    assert count == 2
    assert lines[0] == "INDEX\tENTITY_ID\tFRIENDLY_NAME\tSTATE\tLAST_TRIGGERED"
    assert lines[1].startswith("1\tautomation.first\tFirst Automation\ton\t")
    assert lines[2] == "2\tautomation.second\tSecond\toff\t-"
    assert rest.calls == ["list_states"]
    assert rest.closed is True


class FakeWebSocketClient:
    def __init__(self, config: Any, traces: Any, trace: Any = None) -> None:
        self.config = config
        self.traces = traces
        self.trace = trace
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False

    async def connect(self) -> None:
        self.calls.append(("connect",))

    async def get_automation_config(self, entity_id: str) -> Any:
        self.calls.append(("get_automation_config", entity_id))
        return copy.deepcopy(self.config)

    async def list_traces(self, automation_id: str) -> Any:
        self.calls.append(("list_traces", automation_id))
        return copy.deepcopy(self.traces)

    async def get_trace(self, automation_id: str, run_id: str) -> Any:
        self.calls.append(("get_trace", automation_id, run_id))
        return copy.deepcopy(self.trace)

    async def aclose(self) -> None:
        self.closed = True


def sample_clients() -> tuple[FakeRestClient, FakeWebSocketClient]:
    rest = FakeRestClient(
        {
            "entity_id": "automation.example_arrival",
            "state": "on",
            "attributes": {
                "id": "internal-private-id",
                "friendly_name": "Example arrival",
            },
        }
    )
    websocket = FakeWebSocketClient(
        {
            "config": {
                "id": "internal-private-id",
                "alias": "Example arrival",
                "actions": [
                    {
                        "action": "notify.mobile_app_private_phone",
                        "data": {"message": "Example arrival notification"},
                    }
                ],
            }
        },
        [
            {
                "run_id": "older-private-run-id",
                "state": "stopped",
                "timestamp": {
                    "start": "2026-07-20T10:15:00+00:00",
                    "finish": "2026-07-20T10:15:02+00:00",
                },
            },
            {
                "run_id": "latest-private-run-id",
                "state": "stopped",
                "timestamp": {
                    "start": "2026-07-21T11:25:00+00:00",
                    "finish": "2026-07-21T11:25:03+00:00",
                },
            },
        ],
        {
            "run_id": "latest-private-run-id",
            "trace": {"condition/0": [{"result": {"result": False}}]},
        },
    )
    return rest, websocket


@pytest.mark.asyncio
async def test_capture_without_selection_does_not_download_full_trace(tmp_path: Path) -> None:
    rest, websocket = sample_clients()
    terminal = io.StringIO()

    paths = await capture_automation_diagnostics(
        entity_id="automation.example_arrival",
        rest_client=rest,
        websocket_client=websocket,
        output_dir=tmp_path,
        known_sensitive_values=["dedicated-private-token"],
        output=terminal,
    )

    assert [path.name for path in paths] == [
        "state.json",
        "automation_config.json",
        "trace_list.json",
        "capture_metadata.json",
    ]
    assert all(call[0] != "get_trace" for call in websocket.calls)
    assert rest.closed and websocket.closed
    combined_files = "".join(path.read_text(encoding="utf-8") for path in paths)
    combined_output = terminal.getvalue()
    for private_value in (
        "internal-private-id",
        "older-private-run-id",
        "latest-private-run-id",
        "Example Street",
    ):
        assert private_value not in combined_files
        assert private_value not in combined_output
    assert "automation.example_arrival" in combined_files
    assert "Example arrival" in combined_files
    assert "run_001" in combined_output


@pytest.mark.asyncio
async def test_capture_latest_explicitly_downloads_newest_trace(tmp_path: Path) -> None:
    rest, websocket = sample_clients()

    paths = await capture_automation_diagnostics(
        entity_id="automation.example_arrival",
        latest=True,
        rest_client=rest,
        websocket_client=websocket,
        output_dir=tmp_path,
        output=io.StringIO(),
    )

    assert ("get_trace", "internal-private-id", "latest-private-run-id") in websocket.calls
    assert "trace_get.json" in [path.name for path in paths]
    trace_document = (tmp_path / "trace_get.json").read_text(encoding="utf-8")
    assert "latest-private-run-id" not in trace_document
    assert "run_002" in trace_document


@pytest.mark.asyncio
async def test_capture_run_id_and_overwrite_protection(tmp_path: Path) -> None:
    rest, websocket = sample_clients()
    await capture_automation_diagnostics(
        entity_id="automation.example_arrival",
        run_id="older-private-run-id",
        rest_client=rest,
        websocket_client=websocket,
        output_dir=tmp_path,
        output=io.StringIO(),
    )

    rest_again, websocket_again = sample_clients()
    with pytest.raises(CaptureOperationError) as captured:
        await capture_automation_diagnostics(
            entity_id="automation.example_arrival",
            rest_client=rest_again,
            websocket_client=websocket_again,
            output_dir=tmp_path,
            output=io.StringIO(),
        )

    assert captured.value.operation == "write"
    assert captured.value.error_type == "destination_exists"
    assert rest_again.closed and websocket_again.closed


@pytest.mark.asyncio
async def test_capture_rejects_non_automation_before_using_clients(tmp_path: Path) -> None:
    rest, websocket = sample_clients()

    with pytest.raises(CaptureOperationError) as captured:
        await capture_automation_diagnostics(
            entity_id="sensor.private",
            rest_client=rest,
            websocket_client=websocket,
            output_dir=tmp_path,
            output=io.StringIO(),
        )

    assert captured.value.error_type == "not_automation_entity"
    assert rest.calls == []
    assert websocket.calls == []
    assert rest.closed and websocket.closed
    assert list(tmp_path.iterdir()) == []


class UnsafeAnonymizer(CaptureAnonymizer):
    def anonymize(self, value: Any) -> Any:
        return copy.deepcopy(value)


@pytest.mark.asyncio
async def test_failed_security_audit_writes_no_files(tmp_path: Path) -> None:
    rest, websocket = sample_clients()

    with pytest.raises(CaptureAuditError):
        await capture_automation_diagnostics(
            entity_id="automation.example_arrival",
            rest_client=rest,
            websocket_client=websocket,
            output_dir=tmp_path,
            anonymizer=UnsafeAnonymizer(),
            output=io.StringIO(),
        )

    assert list(tmp_path.iterdir()) == []
    assert rest.closed and websocket.closed
