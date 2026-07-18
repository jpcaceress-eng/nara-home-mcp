from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    ha_url: str = Field(alias="HA_URL")
    ha_token: str = Field(alias="HA_TOKEN")
    mcp_server_name: str = Field(default="Nara Home MCP", alias="MCP_SERVER_NAME")
    mcp_host: str = Field(default="127.0.0.1", alias="MCP_HOST")
    mcp_port: int = Field(default=8000, alias="MCP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    entities_file: Path = Field(default=Path("config/entities.yaml"), alias="ENTITIES_FILE")
    request_timeout_seconds: float = Field(default=5.0, alias="REQUEST_TIMEOUT_SECONDS")
    allow_dynamic_entities: bool = Field(default=False, alias="ALLOW_DYNAMIC_ENTITIES")
    allowed_hosts: str = Field(default="", alias="ALLOWED_HOSTS")
    allowed_origins: str = Field(default="", alias="ALLOWED_ORIGINS")
    oauth_metadata_enabled: bool = Field(default=False, alias="OAUTH_METADATA_ENABLED")
    oauth_issuer_url: str | None = Field(default=None, alias="OAUTH_ISSUER_URL")
    oauth_resource_server_url: str | None = Field(default=None, alias="OAUTH_RESOURCE_SERVER_URL")
    oauth_required_scopes: str = Field(default="", alias="OAUTH_REQUIRED_SCOPES")

    @property
    def mcp_base_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}"

    @property
    def mcp_streamable_http_url(self) -> str:
        return f"{self.mcp_base_url}/mcp"

    @property
    def resolved_oauth_issuer_url(self) -> str:
        return self.oauth_issuer_url or self.mcp_base_url

    @property
    def resolved_oauth_resource_server_url(self) -> str:
        return self.oauth_resource_server_url or self.mcp_streamable_http_url

    @property
    def oauth_required_scopes_list(self) -> list[str]:
        return [scope.strip() for scope in self.oauth_required_scopes.split(",") if scope.strip()]

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


def load_entities_config(path: Path) -> EntitiesConfig:
    raw: dict[str, Any] = {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError("entities.yaml must contain a YAML mapping at the top level")
        raw = loaded
    return EntitiesConfig.model_validate(raw)
