from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..clients import HomeAssistantError
from ..repositories import EntityHistoryRepository

MAX_HISTORY_HOURS = 168
MAX_HISTORY_CHANGES = 500


def validate_history_hours(hours: int) -> None:
    if hours < 1 or hours > MAX_HISTORY_HOURS:
        raise ValueError(f"hours must be between 1 and {MAX_HISTORY_HOURS}")


@dataclass(frozen=True)
class EntityHistoryResult:
    payload: dict[str, Any]
    friendly_name: str | None


class EntityHistoryService:
    """Normalize bounded Home Assistant history into a stable public shape."""

    def __init__(self, repository: EntityHistoryRepository) -> None:
        self._repository = repository

    async def get_entity_history(
        self,
        entity_id: str,
        hours: int,
        *,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        result = await self.get_entity_history_result(entity_id, hours, end_time=end_time)
        return result.payload

    async def get_entity_history_result(
        self,
        entity_id: str,
        hours: int,
        *,
        end_time: datetime | None = None,
    ) -> EntityHistoryResult:
        validate_history_hours(hours)

        period_end = end_time or datetime.now(timezone.utc)
        if period_end.tzinfo is None:
            period_end = period_end.replace(tzinfo=timezone.utc)
        period_start = period_end - timedelta(hours=hours)

        current = await self._repository.get_current_state(entity_id)
        raw_history = await self._repository.get_history(entity_id, period_start, period_end)
        points, incomplete = _extract_points(raw_history)
        changes = _normalize_changes(points)

        truncated = len(changes) > MAX_HISTORY_CHANGES
        if truncated:
            changes = changes[-MAX_HISTORY_CHANGES:]

        current_state = current.get("state")
        last_history_state = changes[-1]["new_value"] if changes else None
        current_or_last_known_state = current_state if current_state is not None else last_history_state
        state_source = "current" if current_state is not None else "history" if last_history_state is not None else "none"

        attributes = current.get("attributes")
        unit = attributes.get("unit_of_measurement") if isinstance(attributes, dict) else None
        friendly_name = attributes.get("friendly_name") if isinstance(attributes, dict) else None
        payload = {
            "entity_id": entity_id,
            "period": {
                "hours": hours,
                "start": period_start.isoformat(),
                "end": period_end.isoformat(),
            },
            "current_or_last_known_state": current_or_last_known_state,
            "state_source": state_source,
            "unit": unit,
            "history_available": bool(changes),
            "incomplete": incomplete or truncated,
            "truncated": truncated,
            "change_count": len(changes),
            "changes": changes,
        }
        return EntityHistoryResult(payload=payload, friendly_name=friendly_name)


def _extract_points(raw_history: Any) -> tuple[list[dict[str, Any]], bool]:
    if raw_history == []:
        return [], False
    if not isinstance(raw_history, list) or len(raw_history) != 1 or not isinstance(raw_history[0], list):
        raise HomeAssistantError("Unexpected Home Assistant history response")

    points: list[dict[str, Any]] = []
    incomplete = False
    for point in raw_history[0]:
        if not isinstance(point, dict):
            incomplete = True
            continue
        timestamp = point.get("last_changed") or point.get("last_updated")
        state = point.get("state")
        if not isinstance(timestamp, str) or not timestamp or state is None:
            incomplete = True
            continue
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            incomplete = True
            continue
        points.append({"timestamp": timestamp, "parsed_timestamp": parsed_timestamp, "state": state})

    points.sort(key=lambda item: item["parsed_timestamp"])
    return points, incomplete


def _normalize_changes(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    previous_value: Any = None
    has_previous = False
    for point in points:
        new_value = point["state"]
        if has_previous and new_value == previous_value:
            continue
        changes.append(
            {
                "timestamp": point["timestamp"],
                "previous_value": previous_value if has_previous else None,
                "new_value": new_value,
            }
        )
        previous_value = new_value
        has_previous = True
    return changes
