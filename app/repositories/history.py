from __future__ import annotations

from datetime import datetime
from typing import Any

from ..clients import HomeAssistantClient


class EntityHistoryRepository:
    """Fetch current state and bounded history for one entity."""

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    async def get_current_state(self, entity_id: str) -> dict[str, Any]:
        return await self._client.get_state(entity_id)

    async def get_history(
        self,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> Any:
        return await self._client.get_history_period(
            start_time,
            end_time,
            filter_entity_id=entity_id,
            minimal_response=True,
            no_attributes=True,
        )
