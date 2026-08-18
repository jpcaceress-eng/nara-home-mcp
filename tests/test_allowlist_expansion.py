import pytest
from pathlib import Path

from app.config import EntitiesConfig, Settings
from app.main import _expand_raw_entity_allowlist, load_runtime_entities
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


def _settings_for_entities(path: Path) -> Settings:
    return Settings.model_construct(
        ha_url="http://ha.example.invalid:8123",
        ha_token="example-token",
        entities_file=path,
    )


def test_runtime_entities_missing_config_fails_without_fallback(tmp_path: Path) -> None:
    settings = _settings_for_entities(tmp_path / "missing.yaml")
    with pytest.raises(FileNotFoundError):
        load_runtime_entities(settings)


def test_runtime_entities_invalid_config_fails_without_fallback(tmp_path: Path) -> None:
    configured = tmp_path / "invalid.yaml"
    configured.write_text("editable_automations:\n  - not-an-automation\n", encoding="utf-8")
    settings = _settings_for_entities(configured)
    with pytest.raises(ValueError):
        load_runtime_entities(settings)


def test_runtime_entities_unreadable_config_fails_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "unreadable.yaml"
    configured.write_text("editable_automations: []\n", encoding="utf-8")
    settings = _settings_for_entities(configured)
    original_open = Path.open

    def denied(path: Path, *args: object, **kwargs: object):
        if path == configured:
            raise PermissionError("configured entity file is not readable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(PermissionError, match="not readable"):
        load_runtime_entities(settings)
