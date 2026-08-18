"""Backward-compatible imports for the Home Assistant client."""

from .clients.home_assistant import HomeAssistantClient, HomeAssistantError

__all__ = ["HomeAssistantClient", "HomeAssistantError"]
