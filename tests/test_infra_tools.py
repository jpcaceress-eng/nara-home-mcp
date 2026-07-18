import pytest

from app.config import EntitiesConfig
from app.tools import _safe_get_allowed_entity_snapshot, _summarize_infra_group


class FakeHAClient:
    def __init__(self, states: dict[str, dict], errors: dict[str, str] | None = None) -> None:
        self.states = states
        self.errors = errors or {}

    async def get_state(self, entity_id: str) -> dict:
        if entity_id in self.errors:
            from app.ha_client import HomeAssistantError

            raise HomeAssistantError(self.errors[entity_id])
        return self.states[entity_id]


@pytest.mark.asyncio
async def test_safe_get_allowed_entity_snapshot_returns_none_for_disallowed_entity() -> None:
    entities = EntitiesConfig(allowed_raw_entities=["sensor.ok"])
    client = FakeHAClient(states={})

    result = await _safe_get_allowed_entity_snapshot(client, entities, "sensor.nope")

    assert result is None


@pytest.mark.asyncio
async def test_summarize_infra_group_tolerates_missing_sensor() -> None:
    entities = EntitiesConfig(
        infra={"proxmox": {"cpu": "sensor.proxmox_cpu", "ram": "sensor.proxmox_ram"}},
        allowed_raw_entities=["sensor.proxmox_cpu", "sensor.proxmox_ram"],
    )
    client = FakeHAClient(
        states={
            "sensor.proxmox_cpu": {
                "state": "12",
                "attributes": {"unit_of_measurement": "%", "friendly_name": "CPU"},
            }
        },
        errors={"sensor.proxmox_ram": "Home Assistant 404: Entity not found"},
    )

    result = await _summarize_infra_group(client, entities, "proxmox")

    assert result["cpu"]["state"] == "12"
    assert result["cpu"]["unit"] == "%"
    assert result["ram"] is None
