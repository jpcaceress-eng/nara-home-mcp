import pytest

from app.config import EntitiesConfig
from app.security import (
    NaraSecurityError,
    ensure_allowed_raw_entity,
    resolve_climate_temperature_entity,
    resolve_light,
    resolve_light_name,
)


def test_allowed_raw_entity_accepts_known_entity() -> None:
    entities = EntitiesConfig(allowed_raw_entities=["sensor.ok"])
    assert ensure_allowed_raw_entity(entities, "sensor.ok") == "sensor.ok"


def test_allowed_raw_entity_rejects_unknown_entity() -> None:
    entities = EntitiesConfig(allowed_raw_entities=["sensor.ok"])
    try:
        ensure_allowed_raw_entity(entities, "sensor.nope")
    except NaraSecurityError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("Expected NaraSecurityError")


def test_resolve_light_rejects_unknown_alias() -> None:
    entities = EntitiesConfig()
    try:
        resolve_light(entities, "unknown room")
    except NaraSecurityError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("Expected NaraSecurityError")


def test_resolve_light_name_accepts_entity_id_and_aliases() -> None:
    entities = EntitiesConfig(
        lights={
            "sample_lamp": {
                "entity_id": "light.example_lamp",
                "friendly_name": "Example lamp",
                "aliases": ["demo lamp", "reading lamp", "sample light"],
            }
        }
    )

    assert resolve_light_name(entities, "demo lamp").entity_id == "light.example_lamp"
    assert resolve_light_name(entities, "light.example_lamp").entity_id == "light.example_lamp"
    assert resolve_light_name(entities, "sample light").entity_id == "light.example_lamp"


def test_resolve_light_name_reports_candidates_on_ambiguity() -> None:
    entities = EntitiesConfig(
        lights={
            "sample_lamp_left": {
                "entity_id": "light.example_lamp_left",
                "friendly_name": "Example lamp left",
                "aliases": ["left lamp"],
            },
            "sample_lamp_right": {
                "entity_id": "light.example_lamp_right",
                "friendly_name": "Example lamp right",
                "aliases": ["right lamp"],
            },
        }
    )

    try:
        resolve_light_name(entities, "example lamp")
    except NaraSecurityError as exc:
        message = str(exc)
        assert "ambiguous" in message
        assert "sample_lamp_left" in message or "sample_lamp_right" in message
    else:
        raise AssertionError("Expected NaraSecurityError")


def test_resolve_light_name_rejects_unknown_entity_id() -> None:
    entities = EntitiesConfig(
        lights={
            "sample_lamp": {
                "entity_id": "light.example_lamp",
                "friendly_name": "Example lamp",
                "aliases": ["demo lamp"],
            }
        }
    )

    with pytest.raises(NaraSecurityError) as exc_info:
        resolve_light_name(entities, "light.unlisted_lamp")

    assert "Allowed aliases" in str(exc_info.value)


def test_resolve_climate_temperature_entity_accepts_climate_locations() -> None:
    entities = EntitiesConfig(
        climate={
            "sample_zone": {
                "temperature": "sensor.example_zone_temperature",
                "humidity": "sensor.example_zone_humidity",
            }
        }
    )

    resolved = resolve_climate_temperature_entity(entities, "sample zone")
    assert resolved.entity_id == "sensor.example_zone_temperature"
