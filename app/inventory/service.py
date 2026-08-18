from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .graph import InventoryGraph
from .models import InventorySnapshot
from .scheduler import InventoryScheduler
from .store import InventoryStore


MAX_PAGE_SIZE = 100
RESOURCE_TYPES = frozenset({"entity", "device", "area", "integration", "service"})


class InventoryQueryError(ValueError):
    pass


class InventoryService:
    def __init__(self, store: InventoryStore, scheduler: InventoryScheduler | None = None) -> None:
        self._store = store
        self._scheduler = scheduler

    def status(self) -> dict[str, Any]:
        snapshot = self._store.snapshot
        scheduler_status = self._scheduler.status() if self._scheduler is not None else {
            "last_attempt_at": None,
            "last_success_at": None,
            "next_refresh_at": None,
            "collections": 0,
            "consecutive_failures": 0,
            "refresh_in_progress": False,
        }
        return self._metadata(snapshot) | scheduler_status | {
            "counts": dict(snapshot.counts),
            "errors": [dict(error) for error in snapshot.errors],
            "write_capability": False,
        }

    def search(
        self,
        resource_type: str,
        query: str = "",
        cursor: str | None = None,
        limit: int = 50,
        kind: str | None = None,
        area_id: str | None = None,
        integration_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self._store.snapshot
        records = self._records(snapshot, resource_type)
        needle = query.strip().casefold()
        items = []
        for ref, record in records.items():
            if needle and needle not in _searchable_text(ref, record):
                continue
            if kind and record.get("kind") != kind:
                continue
            if area_id and record.get("area_id") != area_id:
                continue
            if integration_id and not _matches_integration(record, integration_id):
                continue
            items.append({"resource_type": resource_type, "ref": ref, **dict(record)})
        return self._page(snapshot, items, cursor, limit)

    def get_device(
        self, device_id: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        snapshot = self._store.snapshot
        device = snapshot.devices.get(device_id)
        if device is None:
            raise InventoryQueryError("Unknown device")
        entities = [
            {"entity_id": entity_id, **dict(entity)}
            for entity_id, entity in snapshot.entities.items()
            if entity.get("device_id") == device_id
        ]
        page = self._page(snapshot, entities, cursor, limit)
        integration_ids = device.get("config_entries", [])
        return self._metadata(snapshot) | {
            "device": {"device_id": device_id, **dict(device)},
            "area": dict(snapshot.areas[device["area_id"]])
            if device.get("area_id") in snapshot.areas else None,
            "integrations": [
                {"integration_id": entry_id, **dict(snapshot.integrations[entry_id])}
                for entry_id in integration_ids
                if entry_id in snapshot.integrations
            ],
            "entities": page["items"],
            "pagination": page["pagination"],
            "write_capability": False,
        }

    def dependencies(
        self,
        resource_type: str,
        resource_ref: str,
        direction: str = "outgoing",
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if direction not in {"outgoing", "incoming"}:
            raise InventoryQueryError("direction must be outgoing or incoming")
        snapshot = self._store.snapshot
        if resource_type not in RESOURCE_TYPES - {"service"}:
            raise InventoryQueryError("Unsupported resource type")
        graph = InventoryGraph(snapshot)
        relations = [
            {"resource_type": kind, "ref": ref}
            for kind, ref in graph.neighbors(resource_type, resource_ref, direction)
        ]
        return self._page(snapshot, relations, cursor, limit) | {
            "resource_type": resource_type,
            "resource_ref": resource_ref,
            "direction": direction,
            "write_capability": False,
        }

    @staticmethod
    def _records(snapshot: InventorySnapshot, resource_type: str) -> Mapping[str, Mapping[str, Any]]:
        records = {
            "entity": snapshot.entities,
            "device": snapshot.devices,
            "area": snapshot.areas,
            "integration": snapshot.integrations,
            "service": snapshot.services,
        }.get(resource_type)
        if records is None:
            raise InventoryQueryError("Unsupported resource type")
        return records

    def _page(
        self, snapshot: InventorySnapshot, items: list[dict[str, Any]], cursor: str | None, limit: int
    ) -> dict[str, Any]:
        if limit < 1 or limit > MAX_PAGE_SIZE:
            raise InventoryQueryError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        offset = _decode_cursor(cursor, snapshot.generation)
        page = items[offset : offset + limit]
        next_offset = offset + len(page)
        return self._metadata(snapshot) | {
            "items": page,
            "pagination": {
                "limit": limit,
                "returned": len(page),
                "available": len(items),
                "truncated": next_offset < len(items),
                "next_cursor": _encode_cursor(snapshot.generation, next_offset)
                if next_offset < len(items) else None,
            },
        }

    @staticmethod
    def _metadata(snapshot: InventorySnapshot) -> dict[str, Any]:
        age = max(0.0, (datetime.now(timezone.utc) - snapshot.last_checked_at).total_seconds())
        return {
            "inventory_generation": snapshot.generation,
            "inventory_status": snapshot.status,
            "snapshot_created_at": snapshot.created_at.isoformat(),
            "last_checked_at": snapshot.last_checked_at.isoformat(),
            "age_seconds": round(age, 3),
            "partial": snapshot.partial,
        }


def _searchable_text(ref: str, record: Mapping[str, Any]) -> str:
    values = [ref]
    for key in ("name", "name_by_user", "original_name", "domain", "manufacturer", "model", "title"):
        value = record.get(key)
        if isinstance(value, str):
            values.append(value)
    return " ".join(values).casefold()


def _matches_integration(record: Mapping[str, Any], integration_id: str) -> bool:
    return record.get("config_entry_id") == integration_id or integration_id in record.get("config_entries", ())


def _encode_cursor(generation: int, offset: int) -> str:
    raw = json.dumps({"g": generation, "o": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, generation: int) -> int:
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        if payload.get("g") != generation:
            raise InventoryQueryError("Cursor belongs to a stale inventory generation")
        offset = payload.get("o")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except InventoryQueryError:
        raise
    except Exception:
        raise InventoryQueryError("Invalid cursor") from None
