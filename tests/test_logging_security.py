from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from app.clients import HomeAssistantWebSocketClient
from app.clients.home_assistant import HomeAssistantClient, HomeAssistantError
from app.main import THIRD_PARTY_SENSITIVE_LOGGERS, _configure_third_party_logging


INTERNAL_URL = "http://203.0.113.23:8123"
INTERNAL_IP = "203.0.113.23"
PRIVATE_TOKEN = "private-test-token"
PRIVATE_HEADER = "X-Private-Header"
PRIVATE_QUERY = "api_key=private-query-secret"


def _captured_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def _assert_sensitive_values_absent(text: str) -> None:
    for forbidden in (
        INTERNAL_URL,
        INTERNAL_IP,
        PRIVATE_TOKEN,
        PRIVATE_HEADER,
        PRIVATE_QUERY,
        "Authorization",
        "Bearer ",
    ):
        assert forbidden not in text


@pytest.mark.asyncio
async def test_rest_state_and_service_logs_hide_url_ip_token_headers_and_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_third_party_logging()
    caplog.set_level(logging.DEBUG)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {PRIVATE_TOKEN}"
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[])
        assert request.url.path == "/api/services"
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(INTERNAL_URL, PRIVATE_TOKEN)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=INTERNAL_URL,
        headers={
            "Authorization": f"Bearer {PRIVATE_TOKEN}",
            PRIVATE_HEADER: "private-header-value",
        },
    )
    await client.list_states()
    await client.list_services()
    logging.getLogger("httpx").info(
        "HTTP Request: GET %s/api/states?%s Authorization=Bearer %s %s",
        INTERNAL_URL,
        PRIVATE_QUERY,
        PRIVATE_TOKEN,
        PRIVATE_HEADER,
    )
    logging.getLogger("httpcore.connection").info(
        "connecting to %s with Authorization Bearer %s?%s",
        INTERNAL_URL,
        PRIVATE_TOKEN,
        PRIVATE_QUERY,
    )
    await client.aclose()

    _assert_sensitive_values_absent(_captured_text(caplog))


class _LoggingWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({"type": "auth_required"}))
        self.incoming.put_nowait(json.dumps({"type": "auth_ok"}))

    async def send(self, message: str) -> None:
        parsed = json.loads(message)
        if parsed.get("type") == "config/entity_registry/list":
            self.incoming.put_nowait(
                json.dumps(
                    {"id": parsed["id"], "type": "result", "success": True, "result": []}
                )
            )

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_websocket_logs_hide_url_ip_token_headers_and_parameters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_third_party_logging()
    caplog.set_level(logging.DEBUG)
    websocket = _LoggingWebSocket()

    async def factory(*_: Any, **__: Any) -> _LoggingWebSocket:
        logging.getLogger("websockets.client").info(
            "connecting %s?%s %s Authorization=Bearer %s",
            INTERNAL_URL,
            PRIVATE_QUERY,
            PRIVATE_HEADER,
            PRIVATE_TOKEN,
        )
        return websocket

    client = HomeAssistantWebSocketClient(
        INTERNAL_URL,
        PRIVATE_TOKEN,
        connect_factory=factory,
    )
    await client.connect()
    assert await client.list_entity_registry() == []
    await client.aclose()

    _assert_sensitive_values_absent(_captured_text(caplog))


def test_uvicorn_access_is_suppressed_but_application_info_remains(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_third_party_logging()
    caplog.set_level(logging.DEBUG)
    logging.getLogger("uvicorn.access").info(
        '%s - "GET /mcp?%s HTTP/1.1" 200 Authorization=Bearer %s',
        INTERNAL_IP,
        PRIVATE_QUERY,
        PRIVATE_TOKEN,
    )
    logging.getLogger("uvicorn.error").info(
        "Uvicorn running on %s?%s with %s Bearer %s",
        INTERNAL_URL,
        PRIVATE_QUERY,
        PRIVATE_HEADER,
        PRIVATE_TOKEN,
    )
    logging.getLogger("app.main").info("safe MCP application event")

    text = _captured_text(caplog)
    _assert_sensitive_values_absent(text)
    assert "safe MCP application event" in text
    assert logging.getLogger("app.main").getEffectiveLevel() <= logging.INFO
    for logger_name in THIRD_PARTY_SENSITIVE_LOGGERS:
        assert logging.getLogger(logger_name).getEffectiveLevel() >= logging.WARNING


@pytest.mark.asyncio
async def test_rest_error_is_stable_and_excludes_remote_body_url_and_credentials() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "message": f"denied at {INTERNAL_URL}?{PRIVATE_QUERY}",
                "authorization": f"Bearer {PRIVATE_TOKEN}",
            },
        )

    client = HomeAssistantClient(INTERNAL_URL, PRIVATE_TOKEN)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=INTERNAL_URL
    )
    with pytest.raises(HomeAssistantError) as captured:
        await client.list_states()
    await client.aclose()

    assert str(captured.value) == "Home Assistant request failed with status 403"
    _assert_sensitive_values_absent(str(captured.value))
