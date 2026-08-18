"""Authorization and entity-resolution policy."""

from .entities import (
    NaraSecurityError,
    ResolvedEntity,
    SENSITIVE_DOMAINS,
    ensure_allowed_raw_entity,
    entity_domain,
    is_sensitive_domain,
    resolve_climate_temperature_entity,
    resolve_display,
    resolve_light,
    resolve_light_name,
    resolve_presence_entities,
    resolve_room_temperature_entity,
    resolve_scene,
    resolve_switchable_name,
)

__all__ = [
    "NaraSecurityError",
    "ResolvedEntity",
    "SENSITIVE_DOMAINS",
    "ensure_allowed_raw_entity",
    "entity_domain",
    "is_sensitive_domain",
    "resolve_climate_temperature_entity",
    "resolve_display",
    "resolve_light",
    "resolve_light_name",
    "resolve_presence_entities",
    "resolve_room_temperature_entity",
    "resolve_scene",
    "resolve_switchable_name",
]
