from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..clients import HomeAssistantWebSocketClient
from .policy import (
    AREA_FIELDS,
    CONFIG_ENTRY_FIELDS,
    DEVICE_FIELDS,
    ENTITY_FIELDS,
    STATE_FIELDS,
    safe_state,
    select_fields,
)


class InventorySourceError(RuntimeError):
    """One or more inventory sources could not be collected."""


@dataclass(frozen=True, slots=True)
class CollectedInventory:
    sources: dict[str, Any]
    errors: tuple[dict[str, str], ...]


class InventoryCollector:
    """Collect only approved fields from closed, read-only HA commands."""

    def __init__(
        self,
        websocket: HomeAssistantWebSocketClient | None = None,
        *,
        client_factory: Callable[[], HomeAssistantWebSocketClient] | None = None,
    ) -> None:
        if (websocket is None) == (client_factory is None):
            raise ValueError("Provide exactly one websocket or client_factory")
        self._websocket = websocket
        self._client_factory = client_factory

    async def collect(self) -> CollectedInventory:
        websocket = self._client_factory() if self._client_factory is not None else self._websocket
        assert websocket is not None
        owns_client = self._client_factory is not None
        try:
            await websocket.connect()
            operations: dict[str, Callable[[], Awaitable[Any]]] = {
                "states": websocket.list_states,
                "services": websocket.list_services,
                "entities": websocket.list_entity_registry,
                "devices": websocket.list_device_registry,
                "areas": websocket.list_area_registry,
                "integrations": websocket.list_config_entries,
            }
            results = await asyncio.gather(
                *(operation() for operation in operations.values()), return_exceptions=True
            )
        finally:
            if owns_client:
                await websocket.aclose()
        sources: dict[str, Any] = {}
        errors: list[dict[str, str]] = []
        for source, result in zip(operations, results, strict=True):
            if isinstance(result, BaseException):
                errors.append({"source": source, "code": "source_unavailable"})
                continue
            try:
                sources[source] = self._sanitize_source(source, result)
            except InventorySourceError:
                errors.append({"source": source, "code": "invalid_response"})
        return CollectedInventory(sources, tuple(errors))

    @staticmethod
    def _sanitize_source(source: str, result: Any) -> Any:
        field_policy = {
            "states": STATE_FIELDS,
            "entities": ENTITY_FIELDS,
            "devices": DEVICE_FIELDS,
            "areas": AREA_FIELDS,
            "integrations": CONFIG_ENTRY_FIELDS,
        }
        if source in field_policy:
            if not isinstance(result, list):
                raise InventorySourceError(f"{source} returned an invalid response")
            sanitized = [select_fields(item, field_policy[source]) for item in result]
            if source == "states":
                for item in sanitized:
                    item["state"] = safe_state(item.get("state"))
            return sanitized
        if source == "services":
            if not isinstance(result, dict):
                raise InventorySourceError("services returned an invalid response")
            return {
                str(domain): sorted(str(service) for service in services)
                for domain, services in result.items()
                if isinstance(domain, str) and isinstance(services, dict)
            }
        raise InventorySourceError("unknown inventory source")
