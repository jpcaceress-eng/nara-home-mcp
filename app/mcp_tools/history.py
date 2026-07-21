from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..clients import HomeAssistantClient, HomeAssistantError
from ..configuration import EntitiesConfig
from ..policy import NaraSecurityError, ensure_allowed_raw_entity
from ..repositories import EntityHistoryRepository
from ..services import EntityHistoryService


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
