from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .config import DisplayConfig, EntitiesConfig, SwitchableEntity


class NaraSecurityError(ValueError):
    """Raised when a request targets an entity outside the allowlist."""


SENSITIVE_DOMAINS: frozenset[str] = frozenset(
    {
        "alarm_control_panel",
        "automation",
        "camera",
        "climate",
        "conversation",
        "assist_satellite",
        "lock",
        "media_player",
        "script",
    }
)


@dataclass(frozen=True)
class ResolvedEntity:
    alias: str
    entity_id: str
    kind: str
    friendly_name: str | None = None


def _normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    normalized = normalized.strip().lower()
    for character in (" ", "-", "/"):
        normalized = normalized.replace(character, "_")
    return normalized


def entity_domain(entity_id: str) -> str:
    return entity_id.strip().split(".", 1)[0].lower()


def is_sensitive_domain(entity_id: str) -> bool:
    return entity_domain(entity_id) in SENSITIVE_DOMAINS


def ensure_allowed_raw_entity(entities: EntitiesConfig, entity_id: str) -> str:
    normalized = entity_id.strip()
    if normalized not in entities.allowed_raw_entities:
        raise NaraSecurityError(f"Entity '{entity_id}' is not allowed")
    return normalized


def resolve_room_temperature_entity(entities: EntitiesConfig, room: str) -> ResolvedEntity:
    alias = _normalize_name(room)
    room_config = entities.rooms.get(alias)
    if not room_config or not room_config.temperature:
        raise NaraSecurityError(f"Room '{room}' has no allowed temperature sensor")
    return ResolvedEntity(alias=alias, entity_id=room_config.temperature, kind="temperature")


def resolve_climate_temperature_entity(entities: EntitiesConfig, location: str) -> ResolvedEntity:
    alias = _normalize_name(location)
    climate_config = entities.climate.get(alias)
    if not climate_config or not climate_config.temperature:
        raise NaraSecurityError(f"Climate location '{location}' has no allowed temperature sensor")
    return ResolvedEntity(alias=alias, entity_id=climate_config.temperature, kind="temperature")


def resolve_presence_entities(entities: EntitiesConfig) -> list[ResolvedEntity]:
    resolved: list[ResolvedEntity] = []
    for alias, room_config in entities.rooms.items():
        if room_config.presence:
            resolved.append(ResolvedEntity(alias=alias, entity_id=room_config.presence, kind="presence"))
    if not resolved:
        raise NaraSecurityError("No allowed presence sensors configured")
    return resolved


def _resolve_switchable(
    candidates: dict[str, SwitchableEntity],
    alias_or_name: str,
    kind: str,
) -> ResolvedEntity:
    resolved = resolve_switchable_name(candidates, alias_or_name, kind)
    if resolved is None:
        raise NaraSecurityError(f"{kind.title()} alias '{alias_or_name}' is not allowed")
    return resolved


def resolve_switchable_name(
    candidates: dict[str, SwitchableEntity],
    name: str,
    kind: str,
) -> ResolvedEntity:
    normalized_name = _normalize_name(name)
    if not normalized_name:
        raise NaraSecurityError(f"{kind.title()} name is empty")

    exact_alias = candidates.get(normalized_name)
    if exact_alias:
        return ResolvedEntity(
            alias=normalized_name,
            entity_id=exact_alias.entity_id,
            kind=kind,
            friendly_name=exact_alias.friendly_name,
        )

    for alias, entity in candidates.items():
        if _normalize_name(entity.entity_id) == normalized_name:
            return ResolvedEntity(
                alias=alias,
                entity_id=entity.entity_id,
                kind=kind,
                friendly_name=entity.friendly_name,
            )

    exact_friendly_matches: list[ResolvedEntity] = []
    partial_matches: list[ResolvedEntity] = []
    for alias, entity in candidates.items():
        friendly_name = entity.friendly_name or ""
        normalized_friendly = _normalize_name(friendly_name) if friendly_name else ""
        alias_tokens = {_normalize_name(alias)}
        alias_tokens.update(_normalize_name(alias_name) for alias_name in entity.aliases)
        alias_tokens.add(_normalize_name(entity.entity_id))

        resolved_entity = ResolvedEntity(
            alias=alias,
            entity_id=entity.entity_id,
            kind=kind,
            friendly_name=entity.friendly_name,
        )

        if normalized_friendly and normalized_friendly == normalized_name:
            exact_friendly_matches.append(resolved_entity)
            continue

        if normalized_name in alias_tokens:
            partial_matches.append(resolved_entity)
            continue

        if normalized_friendly and (
            normalized_name in normalized_friendly or normalized_friendly in normalized_name
        ):
            partial_matches.append(resolved_entity)

    matches = exact_friendly_matches or partial_matches
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidate_text = ", ".join(sorted({match.alias for match in matches}))
        raise NaraSecurityError(
            f"{kind.title()} '{name}' is ambiguous. Candidates: {candidate_text}"
        )

    allowed_text = ", ".join(
        sorted(
            {
                candidate
                for candidate in (
                    list(candidates.keys())
                    + [alias_name for entity in candidates.values() for alias_name in entity.aliases]
                )
                if candidate
            }
        )
    )
    raise NaraSecurityError(
        f"{kind.title()} '{name}' is not allowed. Allowed aliases: {allowed_text}"
    )


def resolve_light_name(entities: EntitiesConfig, alias_or_name: str) -> ResolvedEntity:
    return resolve_switchable_name(entities.lights, alias_or_name, "light")


def resolve_light(entities: EntitiesConfig, alias_or_name: str) -> ResolvedEntity:
    return resolve_light_name(entities, alias_or_name)


def resolve_scene(entities: EntitiesConfig, alias_or_name: str) -> ResolvedEntity:
    return _resolve_switchable(entities.scenes, alias_or_name, "scene")


def resolve_display(entities: EntitiesConfig, alias_or_name: str) -> ResolvedEntity:
    alias = _normalize_name(alias_or_name)
    display: DisplayConfig | None = entities.displays.get(alias)
    if not display:
        raise NaraSecurityError(f"Display alias '{alias_or_name}' is not allowed")
    return ResolvedEntity(
        alias=alias,
        entity_id=display.brightness_entity,
        kind="display",
        friendly_name=display.friendly_name,
    )
