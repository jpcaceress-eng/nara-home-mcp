from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.inventory import InventoryNormalizer, InventoryScheduler, InventoryStore
from app.inventory.collector import CollectedInventory
from app.inventory.service import InventoryService
from app.configuration import Settings


def _payload(*, state: str = "1", entity_id: str = "sensor.one") -> CollectedInventory:
    return CollectedInventory(
        {
            "states": [{"entity_id": entity_id, "state": state}],
            "services": {"light": ["turn_on"]},
            "entities": [{"entity_id": entity_id, "platform": "demo"}],
            "devices": [],
            "areas": [],
            "integrations": [],
        },
        (),
    )


class ControlledCollector:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.delay = 0.0
        self.failures = 0
        self.partial_failures = 0
        self.state = "1"
        self.entity_id = "sensor.one"
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.cancelled = 0

    async def collect(self) -> CollectedInventory:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.release is not None:
                await self.release.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.failures:
                self.failures -= 1
                raise RuntimeError("private HA failure")
            payload = _payload(state=self.state, entity_id=self.entity_id)
            if self.partial_failures:
                self.partial_failures -= 1
                return CollectedInventory(payload.sources, ({"source": "devices", "code": "source_unavailable"},))
            return payload
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


def _store(collector: ControlledCollector) -> InventoryStore:
    return InventoryStore(collector, InventoryNormalizer())  # type: ignore[arg-type]


def test_scheduler_defaults_and_environment_are_configurable() -> None:
    defaults = Settings(HA_URL="http://ha.invalid", HA_TOKEN="token", _env_file=None)
    assert defaults.inventory_refresh_interval_seconds == 300
    assert defaults.inventory_refresh_timeout_seconds == 30
    configured = Settings(
        HA_URL="http://ha.invalid",
        HA_TOKEN="token",
        INVENTORY_REFRESH_INTERVAL_SECONDS=120,
        INVENTORY_REFRESH_TIMEOUT_SECONDS=15,
        INVENTORY_RETRY_BASE_SECONDS=2,
        INVENTORY_RETRY_MAX_SECONDS=20,
        INVENTORY_RETRY_JITTER_RATIO=0.1,
        _env_file=None,
    )
    assert (
        configured.inventory_refresh_interval_seconds,
        configured.inventory_refresh_timeout_seconds,
        configured.inventory_retry_base_seconds,
        configured.inventory_retry_max_seconds,
        configured.inventory_retry_jitter_ratio,
    ) == (120, 15, 2, 20, 0.1)


async def _wait_for(predicate: Any, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_several_automatic_cycles_and_status_fields() -> None:
    collector = ControlledCollector()
    store = _store(collector)
    scheduler = InventoryScheduler(store, interval_seconds=0.01, timeout_seconds=1)
    task = asyncio.create_task(scheduler.run())
    await _wait_for(lambda: collector.calls >= 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = scheduler.status()
    assert status["collections"] >= 3
    assert status["last_attempt_at"] is not None
    assert status["last_success_at"] is not None
    assert status["next_refresh_at"] is None
    assert status["consecutive_failures"] == 0
    assert status["refresh_in_progress"] is False


@pytest.mark.asyncio
async def test_queries_never_trigger_refresh_even_while_collection_is_running() -> None:
    collector = ControlledCollector()
    store = _store(collector)
    await store.refresh()
    collector.entered.clear()
    collector.release = asyncio.Event()
    scheduler = InventoryScheduler(store, interval_seconds=60, timeout_seconds=1)
    task = asyncio.create_task(scheduler.run())
    await collector.entered.wait()
    service = InventoryService(store, scheduler)
    generation = store.snapshot.generation
    results = await asyncio.gather(*(
        asyncio.to_thread(service.status) for _ in range(25)
    ))
    assert all(result["inventory_generation"] == generation for result in results)
    assert collector.calls == 2
    collector.release.set()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_store_single_flight_prevents_overlapping_collections() -> None:
    collector = ControlledCollector()
    collector.delay = 0.01
    store = _store(collector)
    await asyncio.gather(store.refresh(), store.refresh(), store.refresh())
    assert collector.calls == 3
    assert collector.max_active == 1


@pytest.mark.asyncio
async def test_timeout_preserves_snapshot_then_backoff_recovers() -> None:
    collector = ControlledCollector()
    store = _store(collector)
    valid = await store.refresh()
    collector.delay = 0.05
    scheduler = InventoryScheduler(
        store, interval_seconds=60, timeout_seconds=0.005,
        retry_base_seconds=0.005, retry_max_seconds=0.01, jitter_ratio=0,
    )
    task = asyncio.create_task(scheduler.run())
    await _wait_for(lambda: scheduler.status()["consecutive_failures"] >= 2)
    assert store.snapshot is valid
    collector.delay = 0
    collector.state = "2"
    await _wait_for(lambda: scheduler.status()["last_success_at"] is not None)
    assert scheduler.status()["consecutive_failures"] == 0
    assert store.snapshot.generation == valid.generation
    assert store.snapshot.entities["sensor.one"]["state"] == "2"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_total_and_partial_failures_retry_and_recover() -> None:
    collector = ControlledCollector()
    collector.failures = 1
    collector.partial_failures = 1
    store = _store(collector)
    scheduler = InventoryScheduler(
        store, interval_seconds=60, timeout_seconds=1,
        retry_base_seconds=0.002, retry_max_seconds=0.004, jitter_ratio=0,
    )
    task = asyncio.create_task(scheduler.run())
    await _wait_for(lambda: scheduler.status()["last_success_at"] is not None)
    assert collector.calls == 3
    assert scheduler.status()["consecutive_failures"] == 0
    assert store.snapshot.status == "ready"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_shutdown_cancels_in_progress_collection_cleanly() -> None:
    collector = ControlledCollector()
    collector.release = asyncio.Event()
    scheduler = InventoryScheduler(_store(collector), interval_seconds=60, timeout_seconds=30)
    task = asyncio.create_task(scheduler.run())
    await collector.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert collector.cancelled == 1
    assert collector.active == 0
    assert scheduler.status()["refresh_in_progress"] is False
    assert scheduler.status()["consecutive_failures"] == 0


def test_service_background_runner_stops_during_collection() -> None:
    collector = ControlledCollector()
    collector.delay = 30
    scheduler = InventoryScheduler(_store(collector), interval_seconds=60, timeout_seconds=60)
    scheduler.start_background()
    deadline = time.monotonic() + 1
    while not scheduler.status()["refresh_in_progress"] and time.monotonic() < deadline:
        time.sleep(0.001)
    scheduler.stop_background(timeout=1)
    assert collector.cancelled == 1
    assert scheduler.status()["refresh_in_progress"] is False


@pytest.mark.asyncio
async def test_volatile_and_structural_cycles_preserve_cursor_semantics() -> None:
    collector = ControlledCollector()
    store = _store(collector)
    first = await store.refresh()
    service = InventoryService(store)
    collector.state = "volatile"
    volatile = await store.refresh()
    assert volatile.generation == first.generation
    collector.entity_id = "sensor.structural"
    structural = await store.refresh()
    repeated = await store.refresh()
    assert structural.generation == first.generation + 1
    assert repeated.generation == structural.generation
