"""External service clients."""

from .home_assistant import HomeAssistantClient, HomeAssistantError
from .home_assistant_websocket import (
    READ_ONLY_COMMANDS,
    HomeAssistantWebSocketAuthenticationError,
    HomeAssistantWebSocketClient,
    HomeAssistantWebSocketCommandError,
    HomeAssistantWebSocketCommandNotAllowedError,
    HomeAssistantWebSocketConnectionClosedError,
    HomeAssistantWebSocketError,
    HomeAssistantWebSocketProtocolError,
    HomeAssistantWebSocketTimeoutError,
)

__all__ = [
    "READ_ONLY_COMMANDS",
    "HomeAssistantClient",
    "HomeAssistantError",
    "HomeAssistantWebSocketAuthenticationError",
    "HomeAssistantWebSocketClient",
    "HomeAssistantWebSocketCommandError",
    "HomeAssistantWebSocketCommandNotAllowedError",
    "HomeAssistantWebSocketConnectionClosedError",
    "HomeAssistantWebSocketError",
    "HomeAssistantWebSocketProtocolError",
    "HomeAssistantWebSocketTimeoutError",
]
