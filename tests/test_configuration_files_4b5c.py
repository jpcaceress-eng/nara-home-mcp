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


class OneInventoryCollector:
    async def collect(self) -> CollectedInventory:
        return CollectedInventory(
            {
                "states": [{"entity_id": "light.kitchen", "state": "on"}],
                "services": {},
                "entities": [{
                    "entity_id": "light.kitchen", "device_id": "device-1",
                    "area_id": "kitchen", "config_entry_id": "entry-1", "platform": "demo",
                }],
                "devices": [], "areas": [], "integrations": [],
            },
            (),
        )


def _service(root: Path) -> HomeAssistantConfigService:
    store = InventoryStore(OneInventoryCollector(), InventoryNormalizer())  # type: ignore[arg-type]
    asyncio.run(store.refresh())
    return HomeAssistantConfigService(
        HomeAssistantConfigProvider(root, require_cifs=False), store
    )


def _tree(root: Path) -> None:
    (root / "packages" / "nested").mkdir(parents=True)
    (root / "blueprints" / "automation").mkdir(parents=True)
    (root / "themes").mkdir()
    (root / ".storage").mkdir()
    (root / "configuration.yaml").write_text(
        "lovelace:\n  dashboards:\n    wall:\n      filename: dashboards/wall.yaml\n"
        "sensor: !include sensors.yaml\napi_key: !secret private_key\n",
        encoding="utf-8",
    )
    (root / "sensors.yaml").write_text("entity: light.kitchen\ncustom: !input target\n", encoding="utf-8")
    (root / "packages" / "nested" / "package.yml").write_text("template: !include_dir_merge_list templates\n", encoding="utf-8")
    (root / "blueprints" / "automation" / "sample.yaml").write_text("blueprint: {}\n", encoding="utf-8")
    (root / "themes" / "night.yaml").write_text("Night: {}\n", encoding="utf-8")
    (root / "automations.yaml").write_text("- alias: Demo\n  entity_id: light.kitchen\n", encoding="utf-8")
    (root / "scripts.yaml").write_text("demo: {}\n", encoding="utf-8")
    (root / "scenes.yaml").write_text("[]\n", encoding="utf-8")
    (root / "templates.yaml").write_text("- sensor: []\n", encoding="utf-8")
    (root / "dashboards").mkdir()
    (root / "dashboards" / "wall.yaml").write_text("views:\n  - cards:\n      - entity: light.kitchen\n", encoding="utf-8")
    (root / "secrets.yaml").write_text("private_key: never-return-this\n", encoding="utf-8")
    (root / ".storage" / "lovelace").write_text(
        json.dumps({"data": {"config": {"views": [{"entity": "light.kitchen", "token": "never"}]}}}),
        encoding="utf-8",
    )
    (root / ".storage" / "lovelace.tablet").write_text(
        json.dumps({"data": {"config": {"views": []}}}), encoding="utf-8"
    )
    (root / ".storage" / "lovelace_dashboards").write_text(
        json.dumps({"data": {"items": [{"url_path": "registry", "filename": "dashboards/registry.yaml"}]}}),
        encoding="utf-8",
    )
    (root / "dashboards" / "registry.yaml").write_text("views: []\n", encoding="utf-8")
    (root / "home-assistant_v2.db").write_bytes(b"binary")
    (root / "home-assistant.log").write_text("ignored", encoding="utf-8")


def test_recursive_yaml_listing_covers_home_assistant_layout_and_paginates(tmp_path: Path) -> None:
    _tree(tmp_path)
    service = _service(tmp_path)
    first = service.list_yaml(limit=3)
    assert first["pagination"]["truncated"] is True
    second = service.list_yaml(first["pagination"]["next_cursor"], limit=100)
    paths = {item["path"] for item in first["items"] + second["items"]}
    assert {
        "configuration.yaml", "automations.yaml", "scripts.yaml", "scenes.yaml",
        "templates.yaml", "packages/nested/package.yml", "blueprints/automation/sample.yaml",
        "themes/night.yaml", "dashboards/wall.yaml", "secrets.yaml",
    }.issubset(paths)
    assert "home-assistant_v2.db" not in paths and "home-assistant.log" not in paths
    assert next(item for item in first["items"] + second["items"] if item["path"] == "secrets.yaml")["readable"] is False


def test_reads_raw_special_tags_and_redacts_secret_references(tmp_path: Path) -> None:
    _tree(tmp_path)
    service = _service(tmp_path)
    configuration = service.read_yaml("configuration.yaml")
    sensors = service.read_yaml("sensors.yaml")
    assert "!include sensors.yaml" in configuration["content"]
    assert "api_key: !secret [referenced]" in configuration["content"]
    assert "private_key" not in configuration["content"]
    assert "!input target" in sensors["content"]
    with pytest.raises(ConfigAccessError, match="Secret files"):
        service.read_yaml("secrets.yaml")


def test_search_is_sanitized_and_relates_dynamic_inventory(tmp_path: Path) -> None:
    _tree(tmp_path)
    service = _service(tmp_path)
    result = service.search_yaml("light.kitchen")
    assert result["pagination"]["available"] >= 2
    relation = result["items"][0]["entities"][0]
    assert relation == {
        "entity_id": "light.kitchen", "known": True, "device_id": "device-1",
        "area_id": "kitchen", "integration_id": "entry-1",
    }
    assert "never-return-this" not in json.dumps(service.search_yaml("never"))


def test_lists_and_reads_yaml_and_storage_dashboards(tmp_path: Path) -> None:
    _tree(tmp_path)
    service = _service(tmp_path)
    listed = service.list_dashboards(limit=100)
    refs = {item["dashboard_ref"] for item in listed["items"]}
    assert "yaml:dashboards/wall.yaml" in refs
    assert "yaml:dashboards/registry.yaml" in refs
    assert "storage:.storage/lovelace" in refs
    assert "storage:.storage/lovelace.tablet" in refs
    yaml_dashboard = service.read_dashboard("yaml:dashboards/wall.yaml")
    storage_dashboard = service.read_dashboard("storage:.storage/lovelace")
    assert yaml_dashboard["entities"][0]["known"] is True
    assert '"token": "[REDACTED]"' in storage_dashboard["content"]
    assert "never" not in storage_dashboard["content"]


def test_path_escapes_absolute_paths_and_symlinks_are_blocked(tmp_path: Path) -> None:
    _tree(tmp_path)
    outside = tmp_path.parent / "outside.yaml"
    outside.write_text("password: exposed", encoding="utf-8")
    (tmp_path / "linked.yaml").symlink_to(outside)
    (tmp_path / "linked_dir").symlink_to(tmp_path.parent, target_is_directory=True)
    service = _service(tmp_path)
    for path in ("../outside.yaml", str(outside), "linked.yaml", "linked_dir/outside.yaml"):
        with pytest.raises(ConfigAccessError):
            service.read_yaml(path)
    paths = {item["path"] for item in service.list_yaml(limit=100)["items"]}
    assert "linked.yaml" not in paths and "linked_dir/outside.yaml" not in paths


def test_large_and_binary_files_are_never_read(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "large.yaml").write_bytes(b"x" * (512 * 1024 + 1))
    (tmp_path / "binary.yaml").write_bytes(b"\xff\xfe")
    service = _service(tmp_path)
    files = {item["path"]: item for item in service.list_yaml(limit=100)["items"]}
    assert files["large.yaml"]["readable"] is False
    with pytest.raises(ConfigAccessError, match="exceeds"):
        service.read_yaml("large.yaml")
    with pytest.raises(ConfigAccessError, match="readable text"):
        service.read_yaml("binary.yaml")


def test_missing_root_is_a_stable_read_only_failure(tmp_path: Path) -> None:
    provider = HomeAssistantConfigProvider(tmp_path / "missing", require_cifs=False)
    assert provider.available is False
    with pytest.raises(ConfigAccessError, match="root is unavailable"):
        provider.yaml_files()


def test_configuration_reads_are_explicitly_disabled_even_for_a_valid_root(tmp_path: Path) -> None:
    _tree(tmp_path)
    provider = HomeAssistantConfigProvider(
        tmp_path, require_cifs=False, enabled=False
    )
    assert provider.available is False
    with pytest.raises(ConfigAccessError, match="root is unavailable"):
        provider.yaml_files()


def test_empty_incomplete_or_non_cifs_root_fails_closed(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert HomeAssistantConfigProvider(empty, require_cifs=False).available is False
    (empty / "configuration.yaml").write_text("homeassistant: {}\n", encoding="utf-8")
    assert HomeAssistantConfigProvider(empty, require_cifs=False).available is False
    (empty / ".storage").mkdir()
    assert HomeAssistantConfigProvider(empty, require_cifs=False).available is True
    production_policy = HomeAssistantConfigProvider(empty)
    assert production_policy.available is False
    with pytest.raises(ConfigAccessError, match="root is unavailable"):
        production_policy.yaml_files()


def test_production_policy_requires_exact_read_only_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path)
    original = Path.read_text

    def mountinfo(path: Path, *args: object, **kwargs: object) -> str:
        if path == Path("/proc/self/mountinfo"):
            return f"42 31 0:52 / {tmp_path} ro,nosuid,nodev,noexec - ext4 /dev/example ro\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mountinfo)
    assert HomeAssistantConfigProvider(tmp_path).available is True

    def writable_mountinfo(path: Path, *args: object, **kwargs: object) -> str:
        if path == Path("/proc/self/mountinfo"):
            return f"42 31 0:52 / {tmp_path} rw,nosuid,nodev,noexec - ext4 /dev/example rw\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", writable_mountinfo)
    assert HomeAssistantConfigProvider(tmp_path).available is False


def test_all_public_operations_are_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path)
    service = _service(tmp_path)

    def forbid_write(*args: object, **kwargs: object) -> None:
        raise AssertionError("configuration access attempted a write")

    monkeypatch.setattr(Path, "write_text", forbid_write)
    monkeypatch.setattr(Path, "write_bytes", forbid_write)
    service.list_yaml(limit=100)
    service.read_yaml("configuration.yaml")
    service.search_yaml("light.kitchen")
    service.list_dashboards(limit=100)
    service.read_dashboard("storage:.storage/lovelace")


def test_configuration_tools_are_part_of_the_43_tool_read_only_catalog(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = InventoryStore(OneInventoryCollector(), InventoryNormalizer())  # type: ignore[arg-type]
    asyncio.run(store.refresh())
    mcp = FastMCP("4b5c-tools")
    ha = HomeAssistantClient("http://ha.invalid", "token")

    class FakeWebSocket:
        async def connect(self) -> None: ...

    register_tools(
        mcp, ha, EntitiesConfig(), automation_websocket=FakeWebSocket(),  # type: ignore[arg-type]
        inventory_store=store,
        ha_config_provider=HomeAssistantConfigProvider(tmp_path, require_cifs=False),
    )
    names = asyncio.run(mcp.list_tools())
    assert len(names) == 43
    added = {
        "ha_list_yaml_files", "ha_read_yaml_file", "ha_search_yaml_files",
        "ha_list_dashboards", "ha_read_dashboard",
    }
    assert added.issubset({tool.name for tool in names})
    assert all("write" not in tool.name for tool in names if tool.name in added)
    asyncio.run(ha.aclose())
