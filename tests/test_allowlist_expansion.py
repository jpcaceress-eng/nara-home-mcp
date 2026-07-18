import pytest

from app.config import EntitiesConfig, Settings
from app.main import _expand_raw_entity_allowlist
from app.security import is_sensitive_domain


class FakeHAClient:
    def __init__(self, states: list[dict]) -> None:
        self.states = states
        self.list_states_calls = 0

    async def list_states(self) -> list[dict]:
        self.list_states_calls += 1
        return self.states


@pytest.mark.asyncio
async def test_expand_raw_entity_allowlist_adds_non_sensitive_entities() -> None:
    entities = EntitiesConfig(allowed_raw_entities=["sensor.keep_me"])
    client = FakeHAClient(
        [
            {"entity_id": "sensor.new_sensor"},
            {"entity_id": "light.example_lamp"},
            {"entity_id": "camera.secret"},
            {"entity_id": "automation.some_rule"},
            {"entity_id": "media_player.tv"},
        ]
    )

    await _expand_raw_entity_allowlist(client, entities, enabled=True)

    assert client.list_states_calls == 1
    assert "sensor.keep_me" in entities.allowed_raw_entities
    assert "sensor.new_sensor" in entities.allowed_raw_entities
    assert "light.example_lamp" in entities.allowed_raw_entities
    assert "camera.secret" not in entities.allowed_raw_entities
    assert "automation.some_rule" not in entities.allowed_raw_entities
    assert "media_player.tv" not in entities.allowed_raw_entities


@pytest.mark.asyncio
async def test_expand_raw_entity_allowlist_is_disabled_by_default() -> None:
    entities = EntitiesConfig(allowed_raw_entities=["sensor.keep_me"])
    client = FakeHAClient([{"entity_id": "sensor.new_sensor"}])

    await _expand_raw_entity_allowlist(client, entities)

    assert client.list_states_calls == 0
    assert entities.allowed_raw_entities == ["sensor.keep_me"]


def test_allow_dynamic_entities_setting_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_URL", "http://ha.example.invalid:8123")
    monkeypatch.setenv("HA_TOKEN", "example-token")
    monkeypatch.delenv("ALLOW_DYNAMIC_ENTITIES", raising=False)

    assert Settings().allow_dynamic_entities is False

    monkeypatch.setenv("ALLOW_DYNAMIC_ENTITIES", "true")
    assert Settings().allow_dynamic_entities is True


def test_sensitive_domain_helper_matches_expected_domains() -> None:
    assert is_sensitive_domain("camera.front_door")
    assert is_sensitive_domain("automation.night_mode")
    assert not is_sensitive_domain("sensor.temperature")
