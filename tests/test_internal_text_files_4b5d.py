from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from app.clients import HomeAssistantClient
from app.configuration import EntitiesConfig
from app.configuration_files import ConfigAccessError, HomeAssistantConfigProvider, HomeAssistantConfigService
from app.inventory import InventoryNormalizer, InventoryStore
from app.inventory.collector import CollectedInventory
from app.tools import register_tools


class InventoryCollector:
    async def collect(self) -> CollectedInventory:
        return CollectedInventory(
            {
                "states": [{"entity_id": "light.kitchen", "state": "on"}],
                "services": {},
                "entities": [{"entity_id": "light.kitchen", "platform": "demo"}],
                "devices": [], "areas": [], "integrations": [],
            },
            (),
        )


def _service(root: Path) -> tuple[HomeAssistantConfigService, InventoryStore]:
    store = InventoryStore(InventoryCollector(), InventoryNormalizer())  # type: ignore[arg-type]
    asyncio.run(store.refresh())
    return HomeAssistantConfigService(
        HomeAssistantConfigProvider(root, require_cifs=False), store
    ), store


def _files(root: Path) -> None:
    (root / ".storage").mkdir()
    (root / "custom_components" / "demo").mkdir(parents=True)
    (root / "www").mkdir()
    (root / "templates").mkdir()
    (root / "themes").mkdir()
    (root / "blueprints").mkdir()
    (root / "backup").mkdir()
    (root / "configuration.yaml").write_text("homeassistant: {}\n", encoding="utf-8")
    (root / "custom_components" / "demo" / "manifest.json").write_text(
        json.dumps({"domain": "demo", "documentation": "https://example.invalid"}), encoding="utf-8"
    )
    (root / "custom_components" / "demo" / "sensor.py").write_text(
        "ENTITY = 'light.kitchen'\nTOKEN = 'eyJabcdefghijklmnopqrstuvwxyz0123456789'\n",
        encoding="utf-8",
    )
    (root / "www" / "card.js").write_text("const entity = 'light.kitchen';\n", encoding="utf-8")
    (root / "www" / "image.png").write_bytes(b"\x89PNG")
    (root / "templates" / "sample.jinja").write_text("{{ states('light.kitchen') }}\n", encoding="utf-8")
    (root / "themes" / "night.yaml").write_text("Night: {}\n", encoding="utf-8")
    (root / "blueprints" / "sample.yaml").write_text("blueprint: {}\n", encoding="utf-8")
    (root / ".storage" / "core.entity_registry").write_text(
        json.dumps({"data": {"entities": [{"entity_id": "light.kitchen", "api_key": "private"}]}}),
        encoding="utf-8",
    )
    (root / ".storage" / "auth").write_text("never-readable", encoding="utf-8")
    (root / ".storage" / "auth_provider.homeassistant").write_text("never-readable", encoding="utf-8")
    (root / "secrets.yaml").write_text("password: never-readable", encoding="utf-8")
    (root / "home-assistant.log").write_text("ignored", encoding="utf-8")
    (root / "home-assistant_v2.db").write_bytes(b"database")
    (root / "backup" / "snapshot.json").write_text("ignored", encoding="utf-8")


def test_lists_useful_internal_text_and_excludes_sensitive_or_binary(tmp_path: Path) -> None:
    _files(tmp_path)
    service, _ = _service(tmp_path)
    paths = {item["path"] for item in service.list_text(limit=100)["items"]}
    assert {
        ".storage/core.entity_registry", "custom_components/demo/manifest.json",
        "custom_components/demo/sensor.py", "www/card.js", "templates/sample.jinja",
        "themes/night.yaml", "blueprints/sample.yaml",
    }.issubset(paths)
    assert not {
        ".storage/auth", ".storage/auth_provider.homeassistant", "secrets.yaml",
        "www/image.png", "home-assistant.log", "home-assistant_v2.db",
        "backup/snapshot.json",
    }.intersection(paths)


def test_read_is_paginated_bounded_redacted_and_relates_inventory(tmp_path: Path) -> None:
    _files(tmp_path)
    (tmp_path / "www" / "many.txt").write_text(
        "\n".join(f"line {index} light.kitchen" for index in range(150)), encoding="utf-8"
    )
    service, _ = _service(tmp_path)
    first = service.read_text("www/many.txt", limit=100)
    second = service.read_text("www/many.txt", first["pagination"]["next_cursor"], limit=100)
    assert len(first["items"]) == 100 and len(second["items"]) == 50
    assert first["items"][0]["entities"][0]["known"] is True
    source = service.read_text("custom_components/demo/sensor.py")
    assert "eyJabcdefghijklmnopqrstuvwxyz" not in json.dumps(source)
    assert "[REDACTED]" in json.dumps(source)


def test_storage_read_and_search_redact_credentials(tmp_path: Path) -> None:
    _files(tmp_path)
    service, _ = _service(tmp_path)
    storage = service.read_text(".storage/core.entity_registry")
    rendered = json.dumps(storage)
    assert "private" not in rendered and "[REDACTED]" in rendered
    result = service.search_text("light.kitchen", limit=100)
    assert result["pagination"]["available"] >= 4
    assert any(
        relation["known"]
        for item in result["items"] for relation in item["entities"]
    )


def test_escapes_symlinks_sensitive_files_and_oversized_files_are_blocked(tmp_path: Path) -> None:
    _files(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    (tmp_path / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    service, _ = _service(tmp_path)
    for path in ("../outside.txt", str(outside), "linked.txt", ".storage/auth", "secrets.yaml", "large.txt"):
        with pytest.raises(ConfigAccessError):
            service.read_text(path)


def test_public_internal_text_operations_never_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _files(tmp_path)
    service, _ = _service(tmp_path)

    def forbid(*args: object, **kwargs: object) -> None:
        raise AssertionError("write attempted")

    monkeypatch.setattr(Path, "write_text", forbid)
    monkeypatch.setattr(Path, "write_bytes", forbid)
    service.list_text(limit=100)
    service.read_text("www/card.js")
    service.search_text("light.kitchen")


def test_three_tools_are_added_without_changing_existing_43(tmp_path: Path) -> None:
    _files(tmp_path)
    _, store = _service(tmp_path)
    mcp = FastMCP("4b5d")
    ha = HomeAssistantClient("http://ha.invalid", "token")

    class WebSocket:
        async def connect(self) -> None: ...

    register_tools(
        mcp, ha, EntitiesConfig(), automation_websocket=WebSocket(),  # type: ignore[arg-type]
        inventory_store=store,
        ha_config_provider=HomeAssistantConfigProvider(tmp_path, require_cifs=False),
    )
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert len(names) == 46
    assert {
        "ha_list_config_text_files", "ha_read_config_text_file",
        "ha_search_config_text_files",
    }.issubset(names)
    asyncio.run(ha.aclose())
