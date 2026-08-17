from __future__ import annotations

from collections import defaultdict
from types import MappingProxyType
from typing import Mapping

from .models import InventorySnapshot


class InventoryGraph:
    def __init__(self, snapshot: InventorySnapshot) -> None:
        outgoing: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        incoming: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)

        def link(source: tuple[str, str], target: tuple[str, str]) -> None:
            outgoing[source].append(target)
            incoming[target].append(source)

        for device_id, device in snapshot.devices.items():
            if isinstance(device.get("area_id"), str):
                link(("area", device["area_id"]), ("device", device_id))
            entry_ids = device.get("config_entries", [])
            if isinstance(entry_ids, (list, tuple)):
                for entry_id in entry_ids:
                    if entry_id in snapshot.integrations:
                        link(("device", device_id), ("integration", entry_id))
        for entity_id, entity in snapshot.entities.items():
            device_id = entity.get("device_id")
            area_id = entity.get("area_id")
            entry_id = entity.get("config_entry_id")
            if isinstance(device_id, str):
                link(("device", device_id), ("entity", entity_id))
            elif isinstance(area_id, str):
                link(("area", area_id), ("entity", entity_id))
            if isinstance(entry_id, str):
                link(("entity", entity_id), ("integration", entry_id))
        self.outgoing: Mapping[tuple[str, str], tuple[tuple[str, str], ...]] = MappingProxyType(
            {key: tuple(sorted(values)) for key, values in outgoing.items()}
        )
        self.incoming: Mapping[tuple[str, str], tuple[tuple[str, str], ...]] = MappingProxyType(
            {key: tuple(sorted(values)) for key, values in incoming.items()}
        )

    def neighbors(self, kind: str, ref: str, direction: str) -> tuple[tuple[str, str], ...]:
        index = self.incoming if direction == "incoming" else self.outgoing
        return index.get((kind, ref), ())
