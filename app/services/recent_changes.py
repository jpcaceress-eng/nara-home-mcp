from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from ..configuration import RecentChangesConfig
from .history import EntityHistoryService, validate_history_hours


class RecentChangesService:
    """Combine normalized state transitions from authorized entities."""

    def __init__(
        self,
        history_service: EntityHistoryService,
        config: RecentChangesConfig | None = None,
    ) -> None:
        self._history_service = history_service
        self._config = config or RecentChangesConfig()

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

        changes_before_filtering = len(changes)
        changes, discarded_as_reordered_collection = self._filter_reordered_collections(changes)
        changes, discarded_by_threshold = self._filter_numeric_noise(changes)
        changes, discarded_by_debounce = self._debounce(changes)
        changes.sort(key=_change_timestamp, reverse=True)

        changes_after_filtering = len(changes)
        truncated = self._config.max_results > 0 and len(changes) > self._config.max_results
        if truncated:
            changes = changes[: self._config.max_results]
        truncated_count = changes_after_filtering - len(changes)

        return {
            "period_hours": hours,
            "changes_found": len(changes),
            "changes_before_filtering": changes_before_filtering,
            "changes_after_filtering": changes_after_filtering,
            "discarded_by_threshold": discarded_by_threshold,
            "discarded_by_debounce": discarded_by_debounce,
            "discarded_as_reordered_collection": discarded_as_reordered_collection,
            "truncated": truncated,
            "truncated_count": truncated_count,
            "changes": changes,
        }

    def _filter_reordered_collections(
        self,
        changes: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        unordered_entities = set(self._config.unordered_entities)
        kept: list[dict[str, Any]] = []
        discarded = 0
        for change in changes:
            if change["entity_id"] not in unordered_entities:
                kept.append(change)
                continue
            old_collection = _parse_unordered_collection(change["old_value"])
            new_collection = _parse_unordered_collection(change["new_value"])
            if old_collection is not None and new_collection is not None and old_collection == new_collection:
                discarded += 1
                continue
            kept.append(change)
        return kept, discarded

    def _filter_numeric_noise(
        self,
        changes: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        kept: list[dict[str, Any]] = []
        discarded = 0
        for change in changes:
            threshold = self._config.numeric_delta_by_entity.get(
                change["entity_id"],
                self._config.numeric_delta_by_unit.get(
                    change["unit"],
                    self._config.numeric_delta_default,
                ),
            )
            old_number = _finite_float(change["old_value"])
            new_number = _finite_float(change["new_value"])
            if old_number is not None and new_number is not None and abs(new_number - old_number) < threshold:
                discarded += 1
                continue
            kept.append(change)
        return kept, discarded

    def _debounce(
        self,
        changes: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        if self._config.debounce_seconds == 0:
            return changes, 0

        by_entity: dict[str, list[dict[str, Any]]] = {}
        for change in changes:
            by_entity.setdefault(change["entity_id"], []).append(change)

        kept: list[dict[str, Any]] = []
        discarded = 0
        for entity_changes in by_entity.values():
            entity_changes.sort(key=_change_timestamp)
            group: list[dict[str, Any]] = []
            for change in entity_changes:
                if group and (
                    _change_timestamp(change) - _change_timestamp(group[0])
                ).total_seconds() > self._config.debounce_seconds:
                    emitted, removed = _collapse_debounce_group(group)
                    kept.extend(emitted)
                    discarded += removed
                    group = []
                group.append(change)
            emitted, removed = _collapse_debounce_group(group)
            kept.extend(emitted)
            discarded += removed
        return kept, discarded


def _change_timestamp(change: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(change["timestamp"].replace("Z", "+00:00"))


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_unordered_collection(value: Any) -> tuple[str, ...] | None:
    parsed: Any
    if isinstance(value, list):
        parsed = value
    elif isinstance(value, str) and value.lstrip().startswith("["):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, list):
            return None
    elif isinstance(value, str) and "," in value:
        parsed = [item.strip() for item in value.split(",")]
    else:
        return None

    try:
        canonical_items = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in parsed]
    except (TypeError, ValueError):
        return None
    return tuple(sorted(canonical_items))


def _collapse_debounce_group(
    group: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    if not group:
        return [], 0
    first = group[0]
    last = group[-1]
    if _values_equivalent(first["old_value"], last["new_value"]):
        return [], len(group)
    collapsed = dict(last)
    collapsed["old_value"] = first["old_value"]
    return [collapsed], len(group) - 1


def _values_equivalent(left: Any, right: Any) -> bool:
    left_number = _finite_float(left)
    right_number = _finite_float(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return left == right
