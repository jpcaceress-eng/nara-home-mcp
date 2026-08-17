from __future__ import annotations

import asyncio
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from app.clients import HomeAssistantClient, HomeAssistantWebSocketClient
from app.main import _register_health_route


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "nara_home"


def load_launcher() -> dict[str, Any]:
    return runpy.run_path(str(APP / "run.sh"))


def test_repository_and_app_metadata_are_minimal_and_protected() -> None:
    repository = yaml.safe_load((ROOT / "repository.yaml").read_text(encoding="utf-8"))
    config = yaml.safe_load((APP / "config.yaml").read_text(encoding="utf-8"))

    assert repository == {"name": "Nara Home Apps"}
    assert config["stage"] == "experimental"
    assert "image" not in config
    assert config["arch"] == ["amd64", "aarch64"]
    assert config["host_network"] is False
    assert config["homeassistant_api"] is True
    assert config["hassio_api"] is False
    assert config["docker_api"] is False
    assert config["full_access"] is False
    assert config["apparmor"] is True
    assert config["ports"] == {"8000/tcp": None}
    assert config["watchdog"] == "http://[HOST]:[PORT:8000]/health"
    assert config["map"] == [
        {
            "type": "homeassistant_config",
            "read_only": True,
            "path": "/homeassistant_config",
        }
    ]
    assert config["options"] == {
        "log_level": "INFO",
        "read_internal_config": False,
    }
    assert config["schema"] == {
        "log_level": "list(INFO|WARNING|ERROR|DEBUG)",
        "read_internal_config": "bool",
    }
    assert not (APP / "build.yaml").exists()
    forbidden = {
        "auth_api", "devices", "gpio", "hassio_role", "host_dbus", "host_ipc",
        "host_pid", "host_uts", "journald", "privileged", "realtime", "uart", "udev",
        "usb",
    }
    assert forbidden.isdisjoint(config)


def test_translations_cover_exactly_the_options_and_disabled_port() -> None:
    config = yaml.safe_load((APP / "config.yaml").read_text(encoding="utf-8"))
    for language in ("en", "es"):
        translation = yaml.safe_load(
            (APP / "translations" / f"{language}.yaml").read_text(encoding="utf-8")
        )
        assert set(translation["configuration"]) == set(config["schema"])
        assert set(translation["network"]) == {"8000/tcp"}
        for option in translation["configuration"].values():
            assert set(option) == {"name", "description"}


def test_dockerfile_is_pinned_nonroot_and_runtime_only() -> None:
    dockerfile = (APP / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM ghcr.io/home-assistant/base-debian:trixie" in dockerfile
    assert ":latest" not in dockerfile
    assert 'io.hass.version="${BUILD_VERSION}"' in dockerfile
    assert 'io.hass.type="app"' in dockerfile
    assert 'io.hass.arch="${BUILD_ARCH}"' in dockerfile
    assert "USER root" in dockerfile
    assert "drop_privileges()" in (APP / "run.sh").read_text(encoding="utf-8")
    assert "--only-binary=:all:" in dockerfile
    assert "python3-venv" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "COPY tests" not in dockerfile
    assert "COPY . " not in dockerfile
    assert "pytest" not in (APP / "constraints.txt").read_text(encoding="utf-8").lower()


def test_apparmor_is_enforcing_and_denies_config_writes() -> None:
    profile = (APP / "apparmor.txt").read_text(encoding="utf-8")
    assert "profile nara_home" in profile
    assert "complain" not in profile
    assert "/data/options.json r," in profile
    assert "/homeassistant_config/** r," in profile
    assert "deny /homeassistant_config/** wklx," in profile
    assert "network inet stream," in profile
    assert "network raw" not in profile


def test_launcher_uses_only_supervisor_token_and_fixed_proxy_paths(tmp_path: Path) -> None:
    options = tmp_path / "options.json"
    options.write_text(
        json.dumps({"log_level": "WARNING", "read_internal_config": False}),
        encoding="utf-8",
    )
    build_environment = load_launcher()["build_environment"]
    environment = build_environment(
        options,
        {"SUPERVISOR_TOKEN": "fixture-supervisor-token", "UNRELATED": "kept"},
    )

    assert environment["HA_URL"] == "http://supervisor/core"
    assert environment["HA_WEBSOCKET_URL"] == "ws://supervisor/core/websocket"
    assert environment["HA_TOKEN"] == "fixture-supervisor-token"
    assert environment["HA_CONFIG_ROOT"] == "/homeassistant_config"
    assert environment["HA_CONFIG_READ_ENABLED"] == "false"
    assert environment["HA_CONFIG_REQUIRE_READ_ONLY_MOUNT"] == "true"
    assert "SUPERVISOR_TOKEN" not in environment
    assert "fixture-supervisor-token" not in options.read_text(encoding="utf-8")

    options.write_text(
        json.dumps({"log_level": "INFO", "read_internal_config": True}),
        encoding="utf-8",
    )
    enabled = build_environment(options, {"SUPERVISOR_TOKEN": "fixture-token"})
    assert enabled["HA_CONFIG_READ_ENABLED"] == "true"


def test_launcher_fails_closed_without_supervisor_token(tmp_path: Path) -> None:
    options = tmp_path / "options.json"
    options.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="SUPERVISOR_TOKEN is required"):
        load_launcher()["build_environment"](options, {})


def test_launcher_drops_root_privileges_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("os.setgroups", lambda groups: calls.append(("groups", groups)))
    monkeypatch.setattr("os.setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr("os.setuid", lambda uid: calls.append(("uid", uid)))
    monkeypatch.setattr(
        "pwd.getpwnam", lambda user: SimpleNamespace(pw_uid=999, pw_gid=999)
    )

    load_launcher()["drop_privileges"]()

    assert calls == [("groups", []), ("gid", 999), ("uid", 999)]


@pytest.mark.asyncio
async def test_rest_proxy_get_and_bearer_header_are_exact() -> None:
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(
        "http://supervisor/core", "fixture-supervisor-token", timeout_seconds=1
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://supervisor/core",
        headers={
            "Authorization": "Bearer fixture-supervisor-token",
            "Content-Type": "application/json",
        },
        transport=httpx.MockTransport(handler),
    )
    await client.list_states()
    await client.aclose()

    assert len(observed) == 1
    assert observed[0].method == "GET"
    assert str(observed[0].url) == "http://supervisor/core/api/states"
    assert observed[0].headers["Authorization"] == "Bearer fixture-supervisor-token"


@pytest.mark.asyncio
async def test_explicit_supervisor_websocket_proxy_is_used() -> None:
    captured: dict[str, Any] = {}

    class FakeSocket:
        messages = iter(
            [json.dumps({"type": "auth_required"}), json.dumps({"type": "auth_ok"})]
        )

        async def recv(self) -> str:
            return next(self.messages)

        async def send(self, message: str) -> None:
            captured.setdefault("messages", []).append(json.loads(message))

        async def close(self) -> None:
            return None

    async def connect(url: str, **kwargs: Any) -> FakeSocket:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeSocket()

    client = HomeAssistantWebSocketClient(
        "http://supervisor/core",
        "fixture-supervisor-token",
        websocket_url="ws://supervisor/core/websocket",
        connect_factory=connect,
    )
    await client.connect()
    await client.aclose()

    assert captured["url"] == "ws://supervisor/core/websocket"
    assert captured["messages"] == [
        {"type": "auth", "access_token": "fixture-supervisor-token"}
    ]


def test_health_endpoint_is_local_and_read_only() -> None:
    mcp = FastMCP("Nara Home health", stateless_http=True, json_response=True)
    _register_health_route(mcp)
    with TestClient(mcp.streamable_http_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "write_capability": False}
    assert response.headers["Cache-Control"] == "no-store"
