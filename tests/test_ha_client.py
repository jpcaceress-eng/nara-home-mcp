import httpx
import pytest

from app.ha_client import HomeAssistantClient, HomeAssistantError


@pytest.mark.asyncio
async def test_healthcheck_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "API running."})

    transport = httpx.MockTransport(handler)
    client = HomeAssistantClient("http://ha.local", "token")
    client._client = httpx.AsyncClient(transport=transport, base_url="http://ha.local")
    result = await client.healthcheck()
    assert result["message"] == "API running."
    await client.aclose()


@pytest.mark.asyncio
async def test_get_state_raises_on_http_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Entity not found"})

    transport = httpx.MockTransport(handler)
    client = HomeAssistantClient("http://ha.local", "token")
    client._client = httpx.AsyncClient(transport=transport, base_url="http://ha.local")
    with pytest.raises(HomeAssistantError):
        await client.get_state("sensor.missing")
    await client.aclose()


@pytest.mark.asyncio
async def test_call_service_posts_entity_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/services/light/turn_on"
        assert request.content.decode("utf-8") == '{"entity_id":"light.example_lamp"}'
        return httpx.Response(200, json=[{"entity_id": "light.example_lamp", "state": "on"}])

    transport = httpx.MockTransport(handler)
    client = HomeAssistantClient("http://ha.local", "token")
    client._client = httpx.AsyncClient(transport=transport, base_url="http://ha.local")
    result = await client.call_service("light", "turn_on", "light.example_lamp")
    assert isinstance(result, list)
    await client.aclose()
