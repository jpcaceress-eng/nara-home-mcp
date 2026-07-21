from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..clients import HomeAssistantClient, HomeAssistantError
from ..configuration import EntitiesConfig
from ..policy import NaraSecurityError, ensure_allowed_raw_entity
from ..repositories import EntityHistoryRepository
from ..services import EntityHistoryService, RecentChangesService


def register_history_tool(
    mcp: FastMCP,
    ha: HomeAssistantClient,
    entities: EntitiesConfig,
) -> None:
    service = EntityHistoryService(EntityHistoryRepository(ha))

    @mcp.tool()
    async def ha_get_entity_history(entity_id: str, hours: int = 6) -> dict[str, Any]:
        """Return bounded, filtered history for an explicitly allowlisted entity."""
        try:
            safe_entity_id = ensure_allowed_raw_entity(entities, entity_id)
            return await service.get_entity_history(safe_entity_id, hours)
        except (NaraSecurityError, HomeAssistantError, ValueError) as exc:
            raise ToolError(str(exc)) from exc


def register_recent_changes_tool(
    mcp: FastMCP,
    ha: HomeAssistantClient,
    entities: EntitiesConfig,
) -> None:
    service = RecentChangesService(EntityHistoryService(EntityHistoryRepository(ha)))

    @mcp.tool()
    async def ha_get_recent_changes(hours: int = 12) -> dict[str, Any]:
        """Return recent state transitions across all explicitly allowlisted entities."""
        try:
            safe_entity_ids = [
                ensure_allowed_raw_entity(entities, entity_id)
                for entity_id in dict.fromkeys(entities.allowed_raw_entities)
            ]
            return await service.get_recent_changes(safe_entity_ids, hours)
        except (NaraSecurityError, HomeAssistantError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
