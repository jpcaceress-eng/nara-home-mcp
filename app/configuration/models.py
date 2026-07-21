from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RoomConfig(BaseModel):
    temperature: str | None = None
    presence: str | None = None


class SwitchableEntity(BaseModel):
    entity_id: str
    friendly_name: str | None = None
    aliases: list[str] = Field(default_factory=list)


class DisplayConfig(BaseModel):
    brightness_entity: str
    friendly_name: str | None = None


class ClimateRoomConfig(BaseModel):
    temperature: str | None = None
    humidity: str | None = None
    battery: str | None = None


class RecentChangesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    numeric_delta_default: NonNegativeFloat = 0
    debounce_seconds: int = Field(default=0, ge=0)
    max_results: int = Field(default=0, ge=0)
    numeric_delta_by_entity: dict[str, NonNegativeFloat] = Field(default_factory=dict)
    numeric_delta_by_unit: dict[str, NonNegativeFloat] = Field(default_factory=dict)
    unordered_entities: list[str] = Field(default_factory=list)


class EntitiesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rooms: dict[str, RoomConfig] = Field(default_factory=dict)
    climate: dict[str, ClimateRoomConfig] = Field(default_factory=dict)
    batteries: dict[str, str] = Field(default_factory=dict)
    lights: dict[str, SwitchableEntity] = Field(default_factory=dict)
    displays: dict[str, DisplayConfig] = Field(default_factory=dict)
    scenes: dict[str, SwitchableEntity] = Field(default_factory=dict)
    infra: dict[str, dict[str, str]] = Field(default_factory=dict)
    ups: dict[str, str | None] = Field(default_factory=dict)
    allowed_raw_entities: list[str] = Field(default_factory=list)
    recent_changes: RecentChangesConfig = Field(default_factory=RecentChangesConfig)
