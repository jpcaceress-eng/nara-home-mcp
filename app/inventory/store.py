from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from .collector import InventoryCollector
from .models import InventorySnapshot
from .normalizer import InventoryNormalizer, digest_structural_content, freeze_records


class InventoryStore:
    """Publish immutable inventory snapshots with one atomic assignment."""

    def __init__(self, collector: InventoryCollector, normalizer: InventoryNormalizer) -> None:
        self._collector = collector
        self._normalizer = normalizer
        self._lock = asyncio.Lock()
        now = datetime.now(timezone.utc)
        self._snapshot = InventorySnapshot(0, "", now, now, "empty")

    @property
    def snapshot(self) -> InventorySnapshot:
        return self._snapshot

    async def refresh(self) -> InventorySnapshot:
        """Collect and atomically publish one snapshot.

        The lock is deliberately owned by the store so every current or future
        caller gets the same single-flight guarantee.
        """
        async with self._lock:
            checked_at = datetime.now(timezone.utc)
            try:
                collected = await self._collector.collect()
            except Exception:
                current = self._snapshot
                self._snapshot = InventorySnapshot(
                    current.generation,
                    current.content_digest,
                    current.created_at,
                    checked_at,
                    "failed",
                    current.entities,
                    current.devices,
                    current.areas,
                    current.integrations,
                    current.services,
                    (MappingProxyType({"source": "connection", "code": "collection_failed"}),),
                )
                return self._snapshot
            content, digest = self._normalizer.normalize(collected)
            current = self._snapshot
            for error in collected.errors:
                source = error.get("source")
                current_records = getattr(current, source, None)
                if isinstance(current_records, Mapping):
                    content[source] = {
                        key: dict(record) for key, record in current_records.items()
                    }
            digest = digest_structural_content(content)
            if digest == current.content_digest:
                self._snapshot = InventorySnapshot(
                    current.generation, digest, current.created_at, checked_at,
                    "partial" if collected.errors else "ready",
                    freeze_records(content["entities"]),
                    freeze_records(content["devices"]),
                    freeze_records(content["areas"]),
                    freeze_records(content["integrations"]),
                    freeze_records(content["services"]),
                    tuple(MappingProxyType(error) for error in collected.errors),
                )
                return self._snapshot
            self._snapshot = InventorySnapshot(
                current.generation + 1,
                digest,
                checked_at,
                checked_at,
                "partial" if collected.errors else "ready",
                freeze_records(content["entities"]),
                freeze_records(content["devices"]),
                freeze_records(content["areas"]),
                freeze_records(content["integrations"]),
                freeze_records(content["services"]),
                tuple(MappingProxyType(error) for error in collected.errors),
            )
            return self._snapshot
