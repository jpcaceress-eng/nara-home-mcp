from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .history import EntityHistoryService, validate_history_hours


class RecentChangesService:
    """Combine normalized state transitions from authorized entities."""

    def __init__(self, history_service: EntityHistoryService) -> None:
        self._history_service = history_service

    async def get_recent_changes(
        self,
        entity_ids: list[str],
        hours: int,
        *,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        validate_history_hours(hours)

        period_end = end_time or datetime.now(timezone.utc)
        if period_end.tzinfo is None:
            period_end = period_end.replace(tzinfo=timezone.utc)

        changes: list[dict[str, Any]] = []
        for entity_id in entity_ids:
            result = await self._history_service.get_entity_history_result(
                entity_id,
                hours,
                end_time=period_end,
            )
            payload = result.payload
            for change in payload["changes"]:
                if change["previous_value"] is None:
                    continue
                changes.append(
                    {
                        "timestamp": change["timestamp"],
                        "entity_id": entity_id,
                        "friendly_name": result.friendly_name,
                        "old_value": change["previous_value"],
                        "new_value": change["new_value"],
                        "unit": payload["unit"],
                    }
                )

        changes.sort(key=_change_timestamp, reverse=True)
        return {
            "period_hours": hours,
            "changes_found": len(changes),
            "changes": changes,
        }


def _change_timestamp(change: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(change["timestamp"].replace("Z", "+00:00"))
