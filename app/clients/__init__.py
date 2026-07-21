"""External service clients."""

from .home_assistant import HomeAssistantClient, HomeAssistantError

__all__ = ["HomeAssistantClient", "HomeAssistantError"]
