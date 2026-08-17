from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect as websocket_connect


READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "automation/config",
        "config/area_registry/list",
        "config/device_registry/list",
        "config/entity_registry/list",
        "config_entries/get",
        "get_services",
        "get_states",
        "recorder/get_statistics_metadata",
        "recorder/list_statistic_ids",
        "recorder/statistics_during_period",
        "trace/get",
        "trace/list",
    }
)

# The real entity registry exceeds 1 MiB at roughly one thousand entries.
# Keep a strict upper bound while allowing the approved full-registry reads.
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1_048_576


class HomeAssistantWebSocketError(RuntimeError):
    """Base error for the Home Assistant WebSocket transport."""

    def __init__(self, operation: str, error_type: str) -> None:
        self.operation = operation
        self.error_type = error_type
        super().__init__(f"Home Assistant WebSocket {operation} failed ({error_type})")


class HomeAssistantWebSocketAuthenticationError(HomeAssistantWebSocketError):
    """Raised when the WebSocket authentication handshake fails."""


class HomeAssistantWebSocketProtocolError(HomeAssistantWebSocketError):
    """Raised when Home Assistant sends an invalid protocol message."""


class HomeAssistantWebSocketTimeoutError(HomeAssistantWebSocketError):
    """Raised when authentication or a command exceeds its timeout."""


class HomeAssistantWebSocketCommandNotAllowedError(HomeAssistantWebSocketError):
    """Raised before an unapproved command can be connected or sent."""


class HomeAssistantWebSocketConnectionClosedError(HomeAssistantWebSocketError):
    """Raised when the connection closes while work is pending."""


class HomeAssistantWebSocketCommandError(HomeAssistantWebSocketError):
    """Raised for a sanitized Home Assistant success=false response."""

    def __init__(self, operation: str, error_code: str | None = None) -> None:
        self.error_code = error_code
        error_type = "command_error" if error_code is None else f"command_error:{error_code}"
        super().__init__(operation, error_type)


class _WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


_ConnectFactory = Callable[..., Awaitable[_WebSocketConnection]]
_PendingRequest = tuple[asyncio.Future[Any], str]


class HomeAssistantWebSocketClient:
    """Strictly read-only client for selected Home Assistant WebSocket APIs."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 5.0,
        *,
        authentication_timeout_seconds: float | None = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        connect_factory: _ConnectFactory | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if authentication_timeout_seconds is not None and authentication_timeout_seconds <= 0:
            raise ValueError("authentication_timeout_seconds must be greater than zero")
        if max_message_bytes <= 0:
            raise ValueError("max_message_bytes must be greater than zero")

        self._url = _websocket_url(base_url)
        self._token = token
        self._request_timeout = timeout_seconds
        self._authentication_timeout = authentication_timeout_seconds or timeout_seconds
        self._max_message_bytes = max_message_bytes
        self._connect_factory = connect_factory or websocket_connect
        self._connection: _WebSocketConnection | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._pending: dict[int, _PendingRequest] = {}
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._next_request_id = 1
        self._closing = False

    async def __aenter__(self) -> HomeAssistantWebSocketClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def connect(self) -> None:
        """Open and authenticate a Home Assistant WebSocket connection."""
        if self._connection is not None:
            return
        async with self._connect_lock:
            if self._connection is not None:
                return
            await self._connect_authenticated()

    async def _connect_authenticated(self) -> None:
        """Open one authenticated connection while the caller holds the lock."""

        self._closing = False
        try:
            connection = await self._connect_factory(
                self._url,
                open_timeout=self._authentication_timeout,
                close_timeout=self._authentication_timeout,
                max_size=self._max_message_bytes,
            )
            self._connection = connection
            required = await asyncio.wait_for(
                connection.recv(), timeout=self._authentication_timeout
            )
            required_message = _decode_message(required, "authentication")
            if required_message.get("type") != "auth_required":
                raise HomeAssistantWebSocketProtocolError(
                    "authentication", "expected_auth_required"
                )

            await connection.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth_response = await asyncio.wait_for(
                connection.recv(), timeout=self._authentication_timeout
            )
            auth_message = _decode_message(auth_response, "authentication")
            if auth_message.get("type") == "auth_invalid":
                raise HomeAssistantWebSocketAuthenticationError(
                    "authentication", "auth_invalid"
                )
            if auth_message.get("type") != "auth_ok":
                raise HomeAssistantWebSocketProtocolError(
                    "authentication", "expected_auth_ok"
                )
        except TimeoutError:
            await self._close_connection()
            raise HomeAssistantWebSocketTimeoutError(
                "authentication", "timeout"
            ) from None
        except HomeAssistantWebSocketError:
            await self._close_connection()
            raise
        except Exception:
            await self._close_connection()
            raise HomeAssistantWebSocketConnectionClosedError(
                "authentication", "connection_error"
            ) from None

        self._receiver_task = asyncio.create_task(
            self._receive_messages(), name="home-assistant-websocket-receiver"
        )

    async def get_automation_config(self, entity_id: str) -> Any:
        """Return the loaded configuration for one automation entity."""
        return await self._request(
            "automation/config",
            entity_id=_required_string(entity_id, "entity_id"),
        )

    async def list_entity_registry(self) -> Any:
        """Return the Home Assistant entity registry."""
        return await self._request("config/entity_registry/list")

    async def list_states(self) -> Any:
        """Return current states through the read-only WebSocket API."""
        return await self._request("get_states")

    async def list_services(self) -> Any:
        """Return the available service catalog."""
        return await self._request("get_services")

    async def list_device_registry(self) -> Any:
        """Return the Home Assistant device registry."""
        return await self._request("config/device_registry/list")

    async def list_area_registry(self) -> Any:
        """Return the Home Assistant area registry."""
        return await self._request("config/area_registry/list")

    async def list_config_entries(self) -> Any:
        """Return sanitized-at-collector config-entry metadata."""
        return await self._request("config_entries/get")

    async def list_statistic_ids(self) -> Any:
        """Return every statistic series exposed by Recorder."""
        await self.connect()
        return await self._request("recorder/list_statistic_ids")

    async def get_statistics_metadata(self, statistic_ids: list[str] | None = None) -> Any:
        """Return Recorder statistic metadata, optionally for selected series."""
        await self.connect()
        fields = {"statistic_ids": statistic_ids} if statistic_ids else {}
        return await self._request("recorder/get_statistics_metadata", **fields)

    async def get_statistics_during_period(
        self,
        statistic_ids: list[str],
        start_time: str,
        end_time: str,
        period: str,
    ) -> Any:
        """Return all supported Recorder columns for selected statistic series."""
        await self.connect()
        return await self._request(
            "recorder/statistics_during_period",
            statistic_ids=statistic_ids,
            start_time=start_time,
            end_time=end_time,
            period=period,
            types=["change", "last_reset", "max", "mean", "min", "state", "sum"],
        )

    async def list_traces(self, automation_id: str) -> Any:
        """Return trace summaries for one Home Assistant automation ID."""
        return await self._request(
            "trace/list",
            domain="automation",
            item_id=_required_string(automation_id, "automation_id"),
        )

    async def get_trace(self, automation_id: str, run_id: str) -> Any:
        """Return one complete trace for a Home Assistant automation ID."""
        return await self._request(
            "trace/get",
            domain="automation",
            item_id=_required_string(automation_id, "automation_id"),
            run_id=_required_string(run_id, "run_id"),
        )

    async def aclose(self) -> None:
        """Close the transport and fail any outstanding requests."""
        self._closing = True
        receiver = self._receiver_task
        self._receiver_task = None
        if receiver is not None:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        self._fail_pending("close", "client_closed")
        await self._close_connection()

    async def _request(self, command: str, **fields: Any) -> Any:
        _ensure_allowed_command(command)
        connection = self._connection
        if connection is None or self._receiver_task is None or self._receiver_task.done():
            raise HomeAssistantWebSocketConnectionClosedError(
                command, "not_connected"
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        request_id: int | None = None
        try:
            async with asyncio.timeout(self._request_timeout):
                async with self._send_lock:
                    request_id = self._next_request_id
                    self._next_request_id += 1
                    self._pending[request_id] = (future, command)
                    message = {"id": request_id, "type": command, **fields}
                    try:
                        await connection.send(json.dumps(message))
                    except Exception:
                        self._pending.pop(request_id, None)
                        raise HomeAssistantWebSocketConnectionClosedError(
                            command, "send_failed"
                        ) from None
                return await asyncio.shield(future)
        except TimeoutError:
            if request_id is not None:
                self._pending.pop(request_id, None)
            future.cancel()
            raise HomeAssistantWebSocketTimeoutError(command, "timeout") from None

    async def _receive_messages(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            while True:
                raw_message = await connection.recv()
                message = _decode_message(raw_message, "receive")
                if message.get("type") != "result" or not isinstance(message.get("id"), int):
                    raise HomeAssistantWebSocketProtocolError(
                        "receive", "invalid_result"
                    )
                pending = self._pending.pop(message["id"], None)
                if pending is None:
                    continue
                future, operation = pending
                if future.done():
                    continue
                if message.get("success") is True:
                    future.set_result(message.get("result"))
                    continue
                error = message.get("error")
                error_code = error.get("code") if isinstance(error, dict) else None
                future.set_exception(
                    HomeAssistantWebSocketCommandError(
                        operation,
                        str(error_code) if error_code is not None else None,
                    )
                )
        except asyncio.CancelledError:
            raise
        except HomeAssistantWebSocketError as exc:
            self._fail_pending(exc.operation, exc.error_type)
        except Exception:
            if not self._closing:
                self._fail_pending("receive", "unexpected_disconnect")
        finally:
            if not self._closing:
                self._connection = None
                try:
                    await connection.close()
                except Exception:
                    pass

    def _fail_pending(self, operation: str, error_type: str) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future, request_operation in pending:
            if not future.done():
                future.set_exception(
                    HomeAssistantWebSocketConnectionClosedError(
                        request_operation or operation, error_type
                    )
                )

    async def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass


def _ensure_allowed_command(command: str) -> None:
    if command not in READ_ONLY_COMMANDS:
        raise HomeAssistantWebSocketCommandNotAllowedError(
            command or "unknown", "command_not_allowed"
        )


def _decode_message(raw_message: str | bytes, operation: str) -> dict[str, Any]:
    if isinstance(raw_message, bytes):
        raise HomeAssistantWebSocketProtocolError(operation, "binary_message")
    try:
        message = json.loads(raw_message)
    except (TypeError, json.JSONDecodeError):
        raise HomeAssistantWebSocketProtocolError(operation, "invalid_json") from None
    if not isinstance(message, dict):
        raise HomeAssistantWebSocketProtocolError(operation, "invalid_message")
    return message


def _required_string(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _websocket_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme.lower())
    if scheme is None or not parsed.netloc:
        raise ValueError("Home Assistant URL must use http or https")
    return urlunsplit((scheme, parsed.netloc, "/api/websocket", "", ""))
