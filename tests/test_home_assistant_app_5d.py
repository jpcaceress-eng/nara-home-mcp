from __future__ import annotations

import asyncio
import json
import runpy
from pathlib import Path
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
    assert config["init"] is False
    assert config["ports"] == {"8000/tcp": None}
    assert config["watchdog"] == "http://[HOST]:[PORT:8000]/health"
    assert config["map"] == [
        {
            "type": "homeassistant_config",
            "read_only": True,
            "path": "/homeassistant_config",
        }
    ]
    assert "options" not in config
    assert "schema" not in config
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
        assert translation["configuration"] == {}
        assert set(translation["network"]) == {"8000/tcp"}


def test_dockerfile_is_pinned_nonroot_and_runtime_only() -> None:
    dockerfile = (APP / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM ghcr.io/home-assistant/base-debian:trixie" in dockerfile
    assert ":latest" not in dockerfile
    assert 'io.hass.version="${BUILD_VERSION}"' in dockerfile
    assert 'io.hass.type="app"' in dockerfile
    assert 'io.hass.arch="${BUILD_ARCH}"' in dockerfile
    assert "USER 999:999" in dockerfile
    launcher = (APP / "run.sh").read_text(encoding="utf-8")
    assert "validate_runtime_identity()" in launcher
    assert "os.setgroups" not in launcher
    assert "os.setgid" not in launcher
    assert "os.setuid" not in launcher
    assert "--only-binary=:all:" in dockerfile
    assert "python3-venv" in dockerfile
    assert "groupadd --system --gid 999 nara" in dockerfile
    assert "useradd --system --uid 999 --gid 999" in dockerfile
    assert "find /opt/nara -type d -exec chmod 0555 {} +" in dockerfile
    assert "find /opt/nara -type f -exec chmod 0444 {} +" in dockerfile
    assert "find /opt/nara/bin -type f -exec chmod 0555 {} +" in dockerfile
    assert "chmod 777" not in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "COPY tests" not in dockerfile
    assert "COPY . " not in dockerfile
    assert "pytest" not in (APP / "constraints.txt").read_text(encoding="utf-8").lower()

    image_regression = (ROOT / "scripts" / "verify_app_image.py").read_text(
        encoding="utf-8"
    )
    assert image_regression.count('"--cap-drop", "ALL"') == 2
    assert "os.geteuid()}:{os.getegid()}" in image_regression


def test_apparmor_is_enforcing_and_denies_config_writes() -> None:
    profile = (APP / "apparmor.txt").read_text(encoding="utf-8")
    assert "profile nara_home" in profile
    assert "complain" not in profile
    assert "/data/options.json" not in profile
    assert "/opt/nara/pyvenv.cfg r," in profile
    assert "/opt/nara/**" not in profile
    assert "/homeassistant_config/** r," in profile
    assert "deny /homeassistant_config/** wklx," in profile
    assert "network inet stream," in profile
    assert "network raw" not in profile


def test_launcher_uses_only_supervisor_token_and_fixed_safe_settings() -> None:
    build_environment = load_launcher()["build_environment"]
    environment = build_environment(
        {"SUPERVISOR_TOKEN": "fixture-supervisor-token", "UNRELATED": "kept"},
    )

    assert environment["HA_URL"] == "http://supervisor/core"
    assert environment["HA_WEBSOCKET_URL"] == "ws://supervisor/core/websocket"
    assert environment["HA_TOKEN"] == "fixture-supervisor-token"
    assert environment["HA_CONFIG_ROOT"] == "/homeassistant_config"
    assert environment["HA_CONFIG_READ_ENABLED"] == "false"
    assert environment["HA_CONFIG_REQUIRE_READ_ONLY_MOUNT"] == "true"
    assert environment["LOG_LEVEL"] == "INFO"
    assert "SUPERVISOR_TOKEN" not in environment


def test_launcher_fails_closed_without_supervisor_token() -> None:
    with pytest.raises(RuntimeError, match="SUPERVISOR_TOKEN is required"):
        load_launcher()["build_environment"]({})


def test_launcher_accepts_target_identity_without_privileged_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr("os.geteuid", lambda: 999)
    monkeypatch.setattr("os.getegid", lambda: 999)
    monkeypatch.setattr("os.getgroups", lambda: [999])
    monkeypatch.setattr("os.setgroups", lambda groups: calls.append(("groups", groups)))
    monkeypatch.setattr("os.setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr("os.setuid", lambda uid: calls.append(("uid", uid)))

    load_launcher()["validate_runtime_identity"]()

    assert calls == []


@pytest.mark.parametrize(
    ("uid", "gid", "groups"),
    [(0, 0, [0]), (1000, 1000, [1000, 44])],
)
def test_launcher_rejects_unexpected_identity_without_privileged_calls(
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    gid: int,
    groups: list[int],
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr("os.geteuid", lambda: uid)
    monkeypatch.setattr("os.getegid", lambda: gid)
    monkeypatch.setattr("os.getgroups", lambda: groups)
    monkeypatch.setattr("os.setgroups", lambda groups: calls.append(("groups", groups)))
    monkeypatch.setattr("os.setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr("os.setuid", lambda uid: calls.append(("uid", uid)))

    with pytest.raises(
        RuntimeError,
        match=rf"unexpected runtime identity uid={uid} gid={gid}",
    ):
        load_launcher()["validate_runtime_identity"]()

    assert calls == []


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
