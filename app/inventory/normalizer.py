from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from .collector import CollectedInventory


HELPER_DOMAINS = frozenset(
    {
        "counter", "input_boolean", "input_button", "input_datetime", "input_number",
        "input_select", "input_text", "schedule", "template", "timer",
    }
)
PERSONAL_IDENTIFIER_DOMAINS = frozenset({"person", "device_tracker"})


class InventoryNormalizer:
    def normalize(self, collected: CollectedInventory) -> tuple[dict[str, Any], str]:
        states = {
            item["entity_id"]: item.get("state")
            for item in collected.sources.get("states", [])
            if isinstance(item.get("entity_id"), str)
        }
        entities: dict[str, dict[str, Any]] = {}
        for item in collected.sources.get("entities", []):
            raw_entity_id = item.get("entity_id")
            if not isinstance(raw_entity_id, str) or "." not in raw_entity_id:
                continue
            domain = raw_entity_id.split(".", 1)[0]
            entity_id = _public_entity_id(raw_entity_id, domain)
            kind = domain if domain in {"automation", "script", "scene", "camera"} else (
                "helper" if domain in HELPER_DOMAINS else "entity"
            )
            public_item = dict(item)
            public_item["entity_id"] = entity_id
            if domain in PERSONAL_IDENTIFIER_DOMAINS:
                for field in ("name", "original_name"):
                    public_item.pop(field, None)
            entities[entity_id] = {
                **public_item,
                "domain": domain,
                "kind": kind,
                "state": states.get(raw_entity_id),
                "state_status": _state_status(item, states.get(raw_entity_id), raw_entity_id in states),
            }

        devices = _keyed(collected.sources.get("devices", []), "id")
        areas = _keyed(collected.sources.get("areas", []), "area_id")
        integrations = _keyed(collected.sources.get("integrations", []), "entry_id")
        services = {
            f"{domain}.{service}": {"domain": domain, "service": service}
            for domain, names in collected.sources.get("services", {}).items()
            for service in names
        }
        content = {
            "entities": _sorted_nested(entities),
            "devices": _sorted_nested(devices),
            "areas": _sorted_nested(areas),
            "integrations": _sorted_nested(integrations),
            "services": _sorted_nested(services),
        }
        return content, digest_structural_content(content)


def digest_content(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def digest_structural_content(content: Mapping[str, Any]) -> str:
    """Digest only topology, classification, and structural metadata."""
    structural: dict[str, dict[str, dict[str, Any]]] = {}
    for resource_type in ("entities", "devices", "areas", "integrations", "services"):
        records = content.get(resource_type, {})
        structural[resource_type] = {
            ref: {
                field: value
                for field, value in record.items()
                if not _is_observational_field(resource_type, field)
            }
            for ref, record in records.items()
        }
    return digest_content(structural)


def _is_observational_field(resource_type: str, field: str) -> bool:
    return (
        resource_type == "entities" and field in {"state", "state_status"}
    ) or (
        resource_type == "integrations" and field == "state"
    )


def freeze_records(records: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType(
        {
            key: MappingProxyType(
                {field: _freeze_value(field_value) for field, field_value in value.items()}
            )
            for key, value in records.items()
        }
    )


def _freeze_value(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    return value


def _keyed(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {
        str(item[key]): dict(item)
        for item in items
        if isinstance(item, dict) and isinstance(item.get(key), str)
    }


def _sorted_nested(value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {key: value[key] for key in sorted(value)}


def _state_status(registry: dict[str, Any], state: Any, loaded: bool) -> str:
    if registry.get("disabled_by") is not None:
        return "disabled"
    if not loaded:
        return "not_loaded"
    if state in {"unavailable", "unknown"}:
        return str(state)
    return "available"


def _public_entity_id(entity_id: str, domain: str) -> str:
    if domain not in PERSONAL_IDENTIFIER_DOMAINS:
        return entity_id
    digest = hashlib.sha256(entity_id.encode()).hexdigest()[:16]
    return f"{domain}.redacted_{digest}"
