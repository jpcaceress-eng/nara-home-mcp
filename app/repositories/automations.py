from __future__ import annotations

from typing import Any

from ..clients import HomeAssistantClient, HomeAssistantWebSocketClient


class AutomationDiagnosticsRepository:
    """Read-only access to Home Assistant automation states, configs, and traces."""

    def __init__(
        self,
        rest_client: HomeAssistantClient,
        websocket_client: HomeAssistantWebSocketClient,
    ) -> None:
        self._rest = rest_client
        self._websocket = websocket_client

    async def list_automation_states(self) -> list[dict[str, Any]]:
        states = await self._rest.list_states()
        return [
            state
            for state in states
            if isinstance(state.get("entity_id"), str)
            and state["entity_id"].startswith("automation.")
        ]

    async def list_states(self) -> list[dict[str, Any]]:
        return await self._rest.list_states()

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        return await self._rest.get_state(entity_id)

    async def get_config(self, entity_id: str) -> Any:
        await self._websocket.connect()
        return await self._websocket.get_automation_config(entity_id)

    async def list_entity_registry(self) -> Any:
        await self._websocket.connect()
        return await self._websocket.list_entity_registry()

    async def list_services(self) -> list[dict[str, Any]]:
        return await self._rest.list_services()

    async def list_traces(self, automation_id: str) -> Any:
        await self._websocket.connect()
        return await self._websocket.list_traces(automation_id)

    async def get_trace(self, automation_id: str, run_id: str) -> Any:
        await self._websocket.connect()
        return await self._websocket.get_trace(automation_id, run_id)
