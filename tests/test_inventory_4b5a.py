from __future__ import annotations

import asyncio
import json
from time import perf_counter
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from app.inventory import InventoryCollector, InventoryGraph, InventoryNormalizer, InventoryStore
from app.mcp_tools.inventory import register_inventory_tools
from app.inventory.service import InventoryQueryError, InventoryService


class FakeInventoryWebSocket:
    def __init__(self, *, entity_count: int = 3) -> None:
        self.connected = 0
        self.closed = 0
        self.fail_source: str | None = None
        self.entities = [
            {
                "entity_id": f"sensor.bulk_{index:04d}",
                "device_id": "device_kitchen" if index < 2 else None,
                "config_entry_id": "entry_demo",
                "platform": "demo",
                "name": f"Sensor {index}",
                "unique_id": f"secret-unique-{index}",
                "options": {"private": "token"},
            }
            for index in range(entity_count)
        ]
        self.states = [
            {"entity_id": item["entity_id"], "state": str(index), "attributes": {"token": "secret"}}
            for index, item in enumerate(self.entities)
        ]
        self.devices = [
            {
                "id": "device_kitchen",
                "area_id": "kitchen",
                "config_entries": ["entry_demo"],
                "name": "Kitchen device",
                "configuration_url": "https://device.example.invalid/status",
                "serial_number": "private-serial",
            }
        ]
        self.areas = [{"area_id": "kitchen", "name": "Kitchen"}]
        self.integrations = [
            {"entry_id": "entry_demo", "domain": "demo", "title": "Demo", "state": "loaded"}
        ]
        self.services = {"light": {"turn_on": {}, "turn_off": {}}}

    async def connect(self) -> None:
        self.connected += 1

    async def aclose(self) -> None:
        self.closed += 1

    async def _value(self, source: str, value: Any) -> Any:
        if self.fail_source == source:
            raise RuntimeError("private remote failure with token")
        return value

    async def list_states(self) -> Any:
        return await self._value("states", self.states)

    async def list_services(self) -> Any:
        return await self._value("services", self.services)

    async def list_entity_registry(self) -> Any:
        return await self._value("entities", self.entities)

    async def list_device_registry(self) -> Any:
        return await self._value("devices", self.devices)

    async def list_area_registry(self) -> Any:
        return await self._value("areas", self.areas)

    async def list_config_entries(self) -> Any:
        return await self._value("integrations", self.integrations)


def _store(fake: FakeInventoryWebSocket) -> InventoryStore:
    return InventoryStore(InventoryCollector(fake), InventoryNormalizer())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_more_than_one_thousand_entities_are_indexed_and_paginated() -> None:
    store = _store(FakeInventoryWebSocket(entity_count=1_205))
    snapshot = await store.refresh()
    assert snapshot.counts["entities"] == 1_205
    first = InventoryService(store).search("entity", limit=100)
    assert len(first["items"]) == 100
    assert first["pagination"]["truncated"] is True
    second = InventoryService(store).search(
        "entity", cursor=first["pagination"]["next_cursor"], limit=100
    )
    assert second["items"][0]["ref"] == "sensor.bulk_0100"


@pytest.mark.asyncio
async def test_response_order_does_not_change_generation() -> None:
    fake = FakeInventoryWebSocket(entity_count=20)
    store = _store(fake)
    first = await store.refresh()
    fake.entities.reverse()
    fake.states.reverse()
    fake.devices.reverse()
    second = await store.refresh()
    assert second.generation == first.generation
    assert second.content_digest == first.content_digest
    assert second.last_checked_at >= first.last_checked_at


@pytest.mark.asyncio
async def test_graph_relates_area_device_entity_and_integration() -> None:
    store = _store(FakeInventoryWebSocket())
    snapshot = await store.refresh()
    graph = InventoryGraph(snapshot)
    assert graph.neighbors("area", "kitchen", "outgoing") == (("device", "device_kitchen"),)
    assert ("entity", "sensor.bulk_0000") in graph.neighbors("device", "device_kitchen", "outgoing")
    assert graph.neighbors("entity", "sensor.bulk_0000", "outgoing") == (("integration", "entry_demo"),)


@pytest.mark.asyncio
async def test_new_automation_is_visible_without_configuration_change() -> None:
    fake = FakeInventoryWebSocket()
    store = _store(fake)
    await store.refresh()
    fake.entities.append(
        {"entity_id": "automation.new_rule", "config_entry_id": "entry_demo", "platform": "automation"}
    )
    fake.states.append({"entity_id": "automation.new_rule", "state": "on"})
    snapshot = await store.refresh()
    assert snapshot.entities["automation.new_rule"]["kind"] == "automation"
    assert "automation.new_rule" in InventoryService(store).search("entity", kind="automation")["items"][0]["ref"]


@pytest.mark.asyncio
async def test_partial_source_failure_is_sanitized_and_published_atomically() -> None:
    fake = FakeInventoryWebSocket()
    store = _store(fake)
    original = store.snapshot
    fake.fail_source = "devices"
    refresh = asyncio.create_task(store.refresh())
    assert store.snapshot is original
    snapshot = await refresh
    assert snapshot.status == "partial"
    assert snapshot.errors == ({"source": "devices", "code": "source_unavailable"},)
    assert "private remote failure" not in json.dumps(InventoryService(store).status())


@pytest.mark.asyncio
async def test_snapshot_is_immutable_and_sensitive_fields_never_enter_it() -> None:
    fake = FakeInventoryWebSocket()
    fake.entities[0]["name"] = "admin@example.com bearer secret"
    store = _store(fake)
    snapshot = await store.refresh()
    serialized = json.dumps(
        {
            "entities": {key: dict(value) for key, value in snapshot.entities.items()},
            "devices": {key: dict(value) for key, value in snapshot.devices.items()},
        },
        default=list,
    )
    assert "secret-unique" not in serialized
    assert "device.example.invalid" not in serialized
    assert "private-serial" not in serialized
    assert "admin@example.com" not in serialized
    with pytest.raises(TypeError):
        snapshot.entities["sensor.bulk_0000"]["state"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        snapshot.generation = 99  # type: ignore[misc]


@pytest.mark.asyncio
async def test_personal_entity_identifiers_are_pseudonymized() -> None:
    fake = FakeInventoryWebSocket(entity_count=0)
    fake.entities.append(
        {"entity_id": "person.private_full_name", "name": "Private Full Name", "platform": "person"}
    )
    fake.states.append({"entity_id": "person.private_full_name", "state": "home"})
    snapshot = await _store(fake).refresh()
    serialized = json.dumps({key: dict(value) for key, value in snapshot.entities.items()})
    assert "private_full_name" not in serialized
    assert "Private Full Name" not in serialized
    assert next(iter(snapshot.entities)).startswith("person.redacted_")


@pytest.mark.asyncio
async def test_stale_and_invalid_cursors_fail_closed() -> None:
    fake = FakeInventoryWebSocket(entity_count=150)
    store = _store(fake)
    await store.refresh()
    cursor = InventoryService(store).search("entity", limit=10)["pagination"]["next_cursor"]
    fake.entities.append({"entity_id": "sensor.new", "platform": "demo"})
    await store.refresh()
    with pytest.raises(InventoryQueryError, match="stale"):
        InventoryService(store).search("entity", cursor=cursor)
    with pytest.raises(InventoryQueryError, match="Invalid"):
        InventoryService(store).search("entity", cursor="not-a-cursor")


@pytest.mark.asyncio
async def test_total_connection_failure_preserves_last_snapshot() -> None:
    fake = FakeInventoryWebSocket()
    store = _store(fake)
    ready = await store.refresh()

    async def fail_connect() -> None:
        raise RuntimeError("token and private URL must not escape")

    fake.connect = fail_connect  # type: ignore[method-assign]
    failed = await store.refresh()
    assert failed.status == "failed"
    assert failed.generation == ready.generation
    assert failed.entities is ready.entities
    assert "token" not in json.dumps(InventoryService(store).status())


@pytest.mark.asyncio
async def test_inventory_performance_with_five_thousand_entities() -> None:
    store = _store(FakeInventoryWebSocket(entity_count=5_000))
    started = perf_counter()
    snapshot = await store.refresh()
    build_seconds = perf_counter() - started
    started = perf_counter()
    page = InventoryService(store).search("entity", query="bulk_49", limit=100)
    search_seconds = perf_counter() - started
    assert snapshot.counts["entities"] == 5_000
    assert page["pagination"]["returned"] == 100
    assert build_seconds < 2.0
    assert search_seconds < 0.25


@pytest.mark.asyncio
async def test_factory_gives_each_collection_an_exclusive_closed_connection() -> None:
    clients: list[FakeInventoryWebSocket] = []

    def factory() -> FakeInventoryWebSocket:
        client = FakeInventoryWebSocket()
        clients.append(client)
        return client

    store = InventoryStore(
        InventoryCollector(client_factory=factory),  # type: ignore[arg-type]
        InventoryNormalizer(),
    )
    await store.refresh()
    await store.refresh()
    assert len(clients) == 2
    assert all(client.connected == 1 and client.closed == 1 for client in clients)


@pytest.mark.asyncio
async def test_one_hundred_queries_do_not_collect_or_change_generation() -> None:
    fake = FakeInventoryWebSocket(entity_count=150)
    store = _store(fake)
    snapshot = await store.refresh()
    service = InventoryService(store)
    cursor = service.search("entity", limit=10)["pagination"]["next_cursor"]

    for _ in range(25):
        assert service.status()["inventory_generation"] == snapshot.generation
        assert service.search("entity", limit=10)["inventory_generation"] == snapshot.generation
        assert service.get_device("device_kitchen")["inventory_generation"] == snapshot.generation
        assert service.dependencies("area", "kitchen")["inventory_generation"] == snapshot.generation

    assert fake.connected == 1
    assert service.search("entity", cursor=cursor, limit=10)["inventory_generation"] == snapshot.generation


@pytest.mark.asyncio
async def test_observational_changes_publish_without_changing_generation_or_cursor() -> None:
    fake = FakeInventoryWebSocket(entity_count=150)
    store = _store(fake)
    first = await store.refresh()
    service = InventoryService(store)
    cursor = service.search("entity", limit=10)["pagination"]["next_cursor"]

    fake.states[0].update(
        {
            "state": "99.9",
            "last_changed": "2099-01-01T00:00:00+00:00",
            "last_updated": "2099-01-01T00:00:01+00:00",
            "context": {"id": "private"},
        }
    )
    fake.integrations[0]["state"] = "setup_retry"
    fake.states.reverse()
    second = await store.refresh()

    assert second.generation == first.generation
    assert second.content_digest == first.content_digest
    assert second.last_checked_at >= first.last_checked_at
    assert second.entities["sensor.bulk_0000"]["state"] == "99.9"
    assert service.search("entity", cursor=cursor, limit=10)["items"][0]["ref"] == "sensor.bulk_0010"


@pytest.mark.asyncio
async def test_add_and_remove_entity_increment_generation_once_each() -> None:
    fake = FakeInventoryWebSocket()
    store = _store(fake)
    initial = await store.refresh()
    fake.entities.append({"entity_id": "sensor.added", "platform": "demo"})
    added = await store.refresh()
    unchanged = await store.refresh()
    fake.entities.pop()
    removed = await store.refresh()

    assert added.generation == initial.generation + 1
    assert unchanged.generation == added.generation
    assert removed.generation == added.generation + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "relation"),
    [
        (lambda fake: fake.areas[0].update({"floor_id": "ground"}), "area"),
        (lambda fake: fake.devices[0].update({"area_id": "utility"}), "device_area"),
        (lambda fake: fake.integrations[0].update({"domain": "changed_demo"}), "integration"),
        (lambda fake: fake.entities[0].update({"device_id": None}), "entity_device"),
        (lambda fake: fake.entities[0].update({"config_entry_id": None}), "entity_integration"),
    ],
)
async def test_structural_metadata_and_relation_changes_increment_once(
    mutation: Any, relation: str
) -> None:
    fake = FakeInventoryWebSocket()
    store = _store(fake)
    first = await store.refresh()
    mutation(fake)
    changed = await store.refresh()
    repeated = await store.refresh()
    assert relation
    assert changed.generation == first.generation + 1
    assert repeated.generation == changed.generation


@pytest.mark.asyncio
async def test_cursor_invalidates_only_after_structural_change() -> None:
    fake = FakeInventoryWebSocket(entity_count=150)
    store = _store(fake)
    await store.refresh()
    service = InventoryService(store)
    cursor = service.search("entity", limit=10)["pagination"]["next_cursor"]
    fake.states[0]["state"] = "42"
    await store.refresh()
    service.search("entity", cursor=cursor, limit=10)

    fake.entities.append({"entity_id": "sensor.structural", "platform": "demo"})
    await store.refresh()
    with pytest.raises(InventoryQueryError, match="stale"):
        service.search("entity", cursor=cursor, limit=10)


@pytest.mark.asyncio
async def test_four_inventory_tools_are_concurrent_snapshot_reads() -> None:
    fake = FakeInventoryWebSocket(entity_count=150)
    store = _store(fake)
    snapshot = await store.refresh()
    mcp = FastMCP("inventory-concurrency-test")
    register_inventory_tools(mcp, InventoryService(store))

    results = await asyncio.gather(
        mcp.call_tool("ha_inventory_status", {}),
        mcp.call_tool("ha_search_inventory", {"resource_type": "entity", "limit": 10}),
        mcp.call_tool("ha_get_device", {"device_id": "device_kitchen"}),
        mcp.call_tool(
            "ha_get_dependencies",
            {"resource_type": "area", "resource_ref": "kitchen"},
        ),
    )
    assert len(results) == 4
    assert store.snapshot is snapshot
    assert fake.connected == 1
