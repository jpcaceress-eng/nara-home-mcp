"""Backward-compatible imports for configuration APIs."""

from .configuration import (
    ClimateRoomConfig,
    DisplayConfig,
    EntitiesConfig,
    RecentChangesConfig,
    RoomConfig,
    Settings,
    SwitchableEntity,
    load_entities_config,
)

__all__ = [
    "ClimateRoomConfig",
    "DisplayConfig",
    "EntitiesConfig",
    "RecentChangesConfig",
    "RoomConfig",
    "Settings",
    "SwitchableEntity",
    "load_entities_config",
]
