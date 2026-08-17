from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.clients import (
    READ_ONLY_COMMANDS,
    HomeAssistantWebSocketAuthenticationError,
    HomeAssistantWebSocketClient,
    HomeAssistantWebSocketCommandError,
    HomeAssistantWebSocketCommandNotAllowedError,
    HomeAssistantWebSocketConnectionClosedError,
    HomeAssistantWebSocketTimeoutError,
)
from app.clients.home_assistant_websocket import DEFAULT_MAX_MESSAGE_BYTES


class FakeWebSocket:
    def __init__(self, incoming: list[dict[str, Any]] | None = None) -> None:
        self.incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        for message in incoming or []:
            self.push(message)

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    def push(self, message: dict[str, Any]) -> None:
        self.incoming.put_nowait(json.dumps(message))

    def disconnect(self) -> None:
        self.incoming.put_nowait(RuntimeError("private disconnect details"))


def connect_factory(
    websocket: FakeWebSocket,
) -> Callable[..., Awaitable[FakeWebSocket]]:
    async def factory(*_: Any, **__: Any) -> FakeWebSocket:
        return websocket

    return factory


def authenticated_socket() -> FakeWebSocket:
    return FakeWebSocket([{"type": "auth_required"}, {"type": "auth_ok"}])


async def wait_for_sent(websocket: FakeWebSocket, count: int) -> None:
    for _ in range(100):
        if len(websocket.sent) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Expected {count} sent messages")


@pytest.mark.asyncio
async def test_authentication_success_and_explicit_command_shape() -> None:
    websocket = authenticated_socket()
    client = HomeAssistantWebSocketClient(
        "https://ha.example.invalid:8123/base-path",
        "secret-token",
        connect_factory=connect_factory(websocket),
    )

    await client.connect()
    request = asyncio.create_task(
        client.get_automation_config("automation.example")
    )
    await wait_for_sent(websocket, 2)
    websocket.push(
        {"id": 1, "type": "result", "success": True, "result": {"config": {}}}
    )

    assert await request == {"config": {}}
    assert websocket.sent == [
        {"type": "auth", "access_token": "secret-token"},
        {
            "id": 1,
            "type": "automation/config",
            "entity_id": "automation.example",
        },
    ]
    await client.aclose()


def test_websocket_message_limit_supports_real_registry_but_remains_bounded() -> None:
    assert DEFAULT_MAX_MESSAGE_BYTES == 8 * 1_048_576


@pytest.mark.asyncio
async def test_concurrent_connects_share_one_authenticated_connection() -> None:
    websocket = authenticated_socket()
    factory_calls = 0

    async def factory(*_: Any, **__: Any) -> FakeWebSocket:
        nonlocal factory_calls
        factory_calls += 1
        await asyncio.sleep(0)
        return websocket

    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid", "token", connect_factory=factory
    )
    await asyncio.gather(*(client.connect() for _ in range(5)))
    assert factory_calls == 1
    assert websocket.sent == [{"type": "auth", "access_token": "token"}]
    await client.aclose()


@pytest.mark.asyncio
async def test_authentication_failure_is_sanitized_and_closes_connection() -> None:
    websocket = FakeWebSocket(
        [
            {"type": "auth_required"},
            {"type": "auth_invalid", "message": "secret remote explanation"},
        ]
    )
    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid",
        "secret-token",
        connect_factory=connect_factory(websocket),
    )

    with pytest.raises(HomeAssistantWebSocketAuthenticationError) as captured:
        await client.connect()

    assert captured.value.operation == "authentication"
    assert captured.value.error_type == "auth_invalid"
    assert "secret remote explanation" not in str(captured.value)
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_authentication_timeout_closes_connection() -> None:
    websocket = FakeWebSocket()
    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid",
        "token",
        authentication_timeout_seconds=0.01,
        connect_factory=connect_factory(websocket),
    )

    with pytest.raises(HomeAssistantWebSocketTimeoutError) as captured:
        await client.connect()

    assert captured.value.operation == "authentication"
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_request_timeout_removes_pending_request() -> None:
    websocket = authenticated_socket()
    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid",
        "token",
        timeout_seconds=0.01,
        connect_factory=connect_factory(websocket),
    )
    await client.connect()

    with pytest.raises(HomeAssistantWebSocketTimeoutError) as captured:
        await client.list_traces("automation-id")

    assert captured.value.operation == "trace/list"
    assert client._pending == {}
    await client.aclose()


@pytest.mark.asyncio
async def test_out_of_order_responses_are_correlated_by_id() -> None:
    websocket = authenticated_socket()
    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid",
        "token",
        connect_factory=connect_factory(websocket),
    )
    await client.connect()

    traces_request = asyncio.create_task(client.list_traces("automation-id"))
    config_request = asyncio.create_task(
        client.get_automation_config("automation.example")
    )
    await wait_for_sent(websocket, 3)
    websocket.push(
        {"id": 2, "type": "result", "success": True, "result": {"kind": "config"}}
    )
    websocket.push(
        {"id": 1, "type": "result", "success": True, "result": [{"kind": "trace"}]}
    )

    assert await traces_request == [{"kind": "trace"}]
    assert await config_request == {"kind": "config"}
    await client.aclose()


@pytest.mark.asyncio
async def test_unsuccessful_result_raises_sanitized_typed_error() -> None:
    websocket = authenticated_socket()
    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid",
        "token",
        connect_factory=connect_factory(websocket),
    )
    await client.connect()
    request = asyncio.create_task(client.list_traces("automation-id"))
    await wait_for_sent(websocket, 2)
    websocket.push(
        {
            "id": 1,
            "type": "result",
            "success": False,
            "error": {"code": "unauthorized", "message": "private entity name"},
        }
    )

    with pytest.raises(HomeAssistantWebSocketCommandError) as captured:
        await request

    assert captured.value.operation == "trace/list"
    assert captured.value.error_code == "unauthorized"
    assert "private entity name" not in str(captured.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_disallowed_command_is_rejected_before_connection_or_send() -> None:
    websocket = authenticated_socket()
    factory_called = False

    async def factory(*_: Any, **__: Any) -> FakeWebSocket:
        nonlocal factory_called
        factory_called = True
        return websocket

    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid", "token", connect_factory=factory
    )

    with pytest.raises(HomeAssistantWebSocketCommandNotAllowedError):
        await client._request("call_service", domain="automation")

    assert factory_called is False
    assert websocket.sent == []
    assert READ_ONLY_COMMANDS == {
        "automation/config",
        "config/area_registry/list",
        "config/device_registry/list",
        "config/entity_registry/list",
        "config_entries/get",
        "get_services",
        "get_states",
        "trace/get",
        "trace/list",
    }


@pytest.mark.asyncio
async def test_entity_registry_uses_explicit_read_only_command() -> None:
    websocket = authenticated_socket()
    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid", "token", connect_factory=connect_factory(websocket)
    )
    await client.connect()
    request = asyncio.create_task(client.list_entity_registry())
    await wait_for_sent(websocket, 2)
    websocket.push({"id": 1, "type": "result", "success": True, "result": []})
    assert await request == []
    assert websocket.sent[1] == {"id": 1, "type": "config/entity_registry/list"}
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "command"),
    [
        ("list_states", "get_states"),
        ("list_services", "get_services"),
        ("list_device_registry", "config/device_registry/list"),
        ("list_area_registry", "config/area_registry/list"),
        ("list_config_entries", "config_entries/get"),
    ],
)
async def test_inventory_commands_have_parameter_free_closed_contracts(
    method_name: str, command: str
) -> None:
    websocket = authenticated_socket()
    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid", "token", connect_factory=connect_factory(websocket)
    )
    await client.connect()
    request = asyncio.create_task(getattr(client, method_name)())
    await wait_for_sent(websocket, 2)
    websocket.push({"id": 1, "type": "result", "success": True, "result": []})
    assert await request == []
    assert websocket.sent[1] == {"id": 1, "type": command}
    await client.aclose()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_context_manager_closes_connection() -> None:
    websocket = authenticated_socket()
    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid",
        "token",
        connect_factory=connect_factory(websocket),
    )

    async with client:
        assert websocket.closed is False

    assert websocket.closed is True
    await client.aclose()
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_unexpected_disconnect_fails_pending_requests_without_details() -> None:
    websocket = authenticated_socket()
    client = HomeAssistantWebSocketClient(
        "https://home-assistant.example.invalid",
        "token",
        connect_factory=connect_factory(websocket),
    )
    await client.connect()
    request = asyncio.create_task(client.get_trace("automation-id", "run-id"))
    await wait_for_sent(websocket, 2)
    websocket.disconnect()

    with pytest.raises(HomeAssistantWebSocketConnectionClosedError) as captured:
        await request

    assert captured.value.operation == "trace/get"
    assert captured.value.error_type == "unexpected_disconnect"
    assert "private disconnect details" not in str(captured.value)
    await client.aclose()
