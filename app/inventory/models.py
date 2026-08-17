from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping


def frozen_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    generation: int
    content_digest: str
    created_at: datetime
    last_checked_at: datetime
    status: str
    entities: Mapping[str, Mapping[str, Any]] = field(default_factory=frozen_mapping)
    devices: Mapping[str, Mapping[str, Any]] = field(default_factory=frozen_mapping)
    areas: Mapping[str, Mapping[str, Any]] = field(default_factory=frozen_mapping)
    integrations: Mapping[str, Mapping[str, Any]] = field(default_factory=frozen_mapping)
    services: Mapping[str, Mapping[str, Any]] = field(default_factory=frozen_mapping)
    errors: tuple[Mapping[str, str], ...] = ()

    @property
    def partial(self) -> bool:
        return bool(self.errors)

    @property
    def counts(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                "entities": len(self.entities),
                "devices": len(self.devices),
                "areas": len(self.areas),
                "integrations": len(self.integrations),
                "services": len(self.services),
            }
        )
