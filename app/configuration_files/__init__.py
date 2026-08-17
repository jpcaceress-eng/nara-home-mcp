"""Read-only access to Home Assistant YAML and dashboard configuration."""

from .provider import ConfigAccessError, HomeAssistantConfigProvider
from .service import HomeAssistantConfigService

__all__ = ["ConfigAccessError", "HomeAssistantConfigProvider", "HomeAssistantConfigService"]
