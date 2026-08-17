import httpx
import pytest

from app.ha_client import HomeAssistantClient, HomeAssistantError


@pytest.mark.asyncio
async def test_healthcheck_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "API running."})

    transport = httpx.MockTransport(handler)
    client = HomeAssistantClient("https://home-assistant.example.invalid", "token")
    client._client = httpx.AsyncClient(transport=transport, base_url="https://home-assistant.example.invalid")
    result = await client.healthcheck()
    assert result["message"] == "API running."
    await client.aclose()


@pytest.mark.asyncio
async def test_get_state_raises_on_http_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Entity not found"})

    transport = httpx.MockTransport(handler)
    client = HomeAssistantClient("https://home-assistant.example.invalid", "token")
    client._client = httpx.AsyncClient(transport=transport, base_url="https://home-assistant.example.invalid")
    with pytest.raises(HomeAssistantError) as captured:
        await client.get_state("sensor.missing")
    assert "Entity not found" not in str(captured.value)
    assert str(captured.value) == "Home Assistant request failed with status 404"
    await client.aclose()


@pytest.mark.asyncio
async def test_list_services_uses_read_only_catalog_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/services"
        assert request.method == "GET"
        return httpx.Response(200, json=[{"domain": "light", "services": {"turn_on": {}}}])

    client = HomeAssistantClient("https://home-assistant.example.invalid", "token")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://home-assistant.example.invalid"
    )
    assert await client.list_services() == [
        {"domain": "light", "services": {"turn_on": {}}}
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_call_service_posts_entity_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/services/light/turn_on"
        assert request.content.decode("utf-8") == '{"entity_id":"light.example_lamp"}'
        return httpx.Response(200, json=[{"entity_id": "light.example_lamp", "state": "on"}])

    transport = httpx.MockTransport(handler)
    client = HomeAssistantClient("https://home-assistant.example.invalid", "token")
    client._client = httpx.AsyncClient(transport=transport, base_url="https://home-assistant.example.invalid")
    result = await client.call_service("light", "turn_on", "light.example_lamp")
    assert isinstance(result, list)
    await client.aclose()
