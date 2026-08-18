from __future__ import annotations

import asyncio
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .store import InventoryStore


class InventoryScheduler:
    """Run inventory refreshes in one cancellable background task."""

    def __init__(
        self,
        store: InventoryStore,
        *,
        interval_seconds: float = 300.0,
        timeout_seconds: float = 30.0,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 60.0,
        jitter_ratio: float = 0.2,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        if interval_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("inventory interval and timeout must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("invalid inventory retry bounds")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("inventory retry jitter must be between zero and one")
        self._store = store
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds
        self._jitter = jitter_ratio
        self._random = random_fn
        self._state_lock = threading.Lock()
        self._last_attempt: datetime | None = None
        self._last_success: datetime | None = None
        self._next_refresh: datetime | None = None
        self._collections = 0
        self._consecutive_failures = 0
        self._refresh_in_progress = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._thread: threading.Thread | None = None
        self._thread_started = threading.Event()
        self._first_attempt_finished = threading.Event()

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "last_attempt_at": self._iso(self._last_attempt),
                "last_success_at": self._iso(self._last_success),
                "next_refresh_at": self._iso(self._next_refresh),
                "collections": self._collections,
                "consecutive_failures": self._consecutive_failures,
                "refresh_in_progress": self._refresh_in_progress,
            }

    async def run(self) -> None:
        """Refresh immediately and continue until this task is cancelled."""
        try:
            delay = 0.0
            while True:
                self._set_next(delay)
                if delay:
                    await asyncio.sleep(delay)
                self._begin_attempt()
                succeeded = False
                cancelled = False
                try:
                    snapshot = await asyncio.wait_for(
                        self._store.refresh(), timeout=self._timeout
                    )
                    succeeded = snapshot.status == "ready"
                except TimeoutError:
                    succeeded = False
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                finally:
                    self._finish_attempt(None if cancelled else succeeded)
                    self._first_attempt_finished.set()
                delay = self._interval if succeeded else self._retry_delay()
        except asyncio.CancelledError:
            with self._state_lock:
                self._next_refresh = None
                self._refresh_in_progress = False
            raise

    def start_background(self) -> None:
        if self._thread is not None:
            raise RuntimeError("inventory scheduler already started")
        self._thread_started.clear()
        self._first_attempt_finished.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="nara-inventory-scheduler",
            daemon=True,
        )
        self._thread.start()
        if not self._thread_started.wait(self._timeout + 1.0):
            raise RuntimeError("inventory scheduler did not start")

    def wait_for_first_attempt(self, timeout: float) -> bool:
        return self._first_attempt_finished.wait(timeout)

    def stop_background(self, timeout: float = 10.0) -> None:
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("inventory scheduler did not stop cleanly")
        self._thread = None

    def _thread_main(self) -> None:
        async def execute() -> None:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self.run())
            self._thread_started.set()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
                self._loop = None

        asyncio.run(execute())

    def _begin_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        with self._state_lock:
            self._last_attempt = now
            self._next_refresh = None
            self._collections += 1
            self._refresh_in_progress = True

    def _finish_attempt(self, succeeded: bool | None) -> None:
        now = datetime.now(timezone.utc)
        with self._state_lock:
            self._refresh_in_progress = False
            if succeeded is True:
                self._last_success = now
                self._consecutive_failures = 0
            elif succeeded is False:
                self._consecutive_failures += 1

    def _retry_delay(self) -> float:
        with self._state_lock:
            exponent = max(0, self._consecutive_failures - 1)
        base = min(self._retry_max, self._retry_base * (2 ** min(exponent, 30)))
        factor = 1 - self._jitter + (2 * self._jitter * self._random())
        return min(self._retry_max, base * factor)

    def _set_next(self, delay: float) -> None:
        next_refresh = datetime.now(timezone.utc) + timedelta(seconds=delay)
        with self._state_lock:
            self._next_refresh = next_refresh

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None
