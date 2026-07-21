from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
