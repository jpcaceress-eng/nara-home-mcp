#!/usr/bin/env python3
"""Exercise the built App image under the production security boundary."""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import tempfile
import time
from pathlib import Path


RUNTIME_PROBE = r"""
import asyncio
import inspect
import json

from mcp.server.fastmcp import FastMCP

from app.clients import HomeAssistantClient
from app.configuration import EntitiesConfig
from app.configuration_files import HomeAssistantConfigProvider
from app.inventory import InventoryCollector, InventoryNormalizer, InventoryStore
from app.tools import register_tools


class FakeWebSocket:
    async def connect(self):
        return None


async def main():
    mcp = FastMCP(name="Nara Home image probe", stateless_http=True, json_response=True)
    client = HomeAssistantClient("https://home-assistant.example.invalid", "probe-token")
    websocket = FakeWebSocket()
    store = InventoryStore(InventoryCollector(websocket), InventoryNormalizer())
    register_tools(
        mcp,
        client,
        EntitiesConfig(),
        automation_websocket=websocket,
        inventory_store=store,
        ha_config_provider=HomeAssistantConfigProvider(enabled=False),
    )
    names = {tool.name for tool in await mcp.list_tools()}
    controls = {
        "ha_turn_on", "ha_turn_off", "ha_set_light_brightness",
        "ha_set_display_brightness", "ha_run_scene", "ha_call_service",
    }
    metadata = await mcp.call_tool("ha_get_server_version", {})
    source = inspect.getsource(HomeAssistantClient)
    result = {
        "tool_count": len(names),
        "control_tool_count": len(names & controls),
        "write_capability": metadata[1]["write_capability"],
        "rest_get_only": all(
            method not in source
            for method in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"')
        ),
    }
    await client.aclose()
    print(json.dumps(result, sort_keys=True))


asyncio.run(main())
"""


def docker(*args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=capture,
        text=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    name = f"nara-image-regression-{secrets.token_hex(6)}"
    private_token = f"fixture-private-token-{secrets.token_hex(16)}"

    with tempfile.TemporaryDirectory(prefix="nara-image-regression-") as directory:
        options = Path(directory) / "options.json"
        options.write_text(
            json.dumps({"log_level": "INFO", "read_internal_config": False}),
            encoding="utf-8",
        )
        options.chmod(0o444)

        try:
            readable = docker(
                "run", "--rm", "--user", "999:999", "--read-only",
                "--entrypoint", "/opt/nara/bin/python", args.image,
                "-c", "from pathlib import Path; Path('/opt/nara/pyvenv.cfg').read_text()",
            )
            assert readable.returncode == 0

            docker(
                "run", "--detach", "--name", name,
                "--user", "999:999", "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "--mount", f"type=bind,src={options},dst=/data/options.json,readonly",
                "--env", f"SUPERVISOR_TOKEN={private_token}",
                args.image,
            )

            deadline = time.monotonic() + 30
            health: dict[str, object] | None = None
            while time.monotonic() < deadline:
                try:
                    response = docker(
                        "exec", name, "/opt/nara/bin/python", "-c",
                        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read().decode())",
                    )
                    health = json.loads(response.stdout)
                    break
                except subprocess.CalledProcessError:
                    time.sleep(1)
            assert health == {"status": "ok", "write_capability": False}

            probe = docker(
                "exec", name, "/opt/nara/bin/python", "-c", RUNTIME_PROBE
            )
            result = json.loads(probe.stdout.strip().splitlines()[-1])
            assert result == {
                "control_tool_count": 0,
                "rest_get_only": True,
                "tool_count": 43,
                "write_capability": False,
            }

            logs = docker("logs", name).stdout
            assert private_token not in logs

            writable = subprocess.run(
                [
                    "docker", "exec", name, "/opt/nara/bin/python", "-c",
                    "from pathlib import Path; Path('/opt/nara/runtime-write-test').write_text('denied')",
                ],
                capture_output=True,
                text=True,
            )
            assert writable.returncode != 0, "UID 999 unexpectedly wrote below /opt/nara"
        finally:
            subprocess.run(
                ["docker", "rm", "--force", name],
                check=False,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    main()
