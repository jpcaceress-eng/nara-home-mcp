from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_fixed


class HomeAssistantError(RuntimeError):
    """Raised when Home Assistant returns an unexpected response."""


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._client = self._build_client()

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout_seconds,
        )

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client.is_closed:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/api/states/{entity_id}")
        if not isinstance(response, dict):
            raise HomeAssistantError("Unexpected Home Assistant state response")
        return response

    async def list_states(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/states")
        if not isinstance(response, list):
            raise HomeAssistantError("Unexpected Home Assistant states response")
        return response

    async def get_history_period(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        *,
        filter_entity_id: str | None = None,
        minimal_response: bool = True,
        no_attributes: bool = True,
    ) -> Any:
        params: dict[str, Any] = {
            "minimal_response": str(minimal_response).lower(),
            "no_attributes": str(no_attributes).lower(),
        }
        if end_time is not None:
            params["end_time"] = end_time.isoformat()
        if filter_entity_id:
            params["filter_entity_id"] = filter_entity_id
        response = await self._request("GET", f"/api/history/period/{start_time.isoformat()}", params=params)
        return response

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        payload: dict[str, Any] = {"entity_id": entity_id}
        if service_data:
            payload.update(service_data)
        response = await self._request("POST", f"/api/services/{domain}/{service}", json=payload)
        if not isinstance(response, (list, dict)):
            raise HomeAssistantError("Unexpected Home Assistant service response")
        return response

    async def healthcheck(self) -> dict[str, Any]:
        response = await self._request("GET", "/api/")
        if not isinstance(response, dict):
            raise HomeAssistantError("Unexpected Home Assistant API root response")
        return response

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_fixed(1),
            retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
            reraise=True,
        ):
            with attempt:
                response = await self._ensure_client().request(method, path, **kwargs)
                if response.status_code >= 400:
                    detail = _extract_error_detail(response)
                    raise HomeAssistantError(f"Home Assistant {response.status_code}: {detail}")
                return response.json() if response.content else {}
        raise HomeAssistantError("Unreachable retry state")


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or "unknown error"

    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            return str(message)
    return str(payload)
