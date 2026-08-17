"""Data access abstractions for Home Assistant resources."""

from .history import EntityHistoryRepository
from .automations import AutomationDiagnosticsRepository

__all__ = ["AutomationDiagnosticsRepository", "EntityHistoryRepository"]
