#!/usr/bin/env python3
"""Build Nara's fixed, least-privilege Home Assistant App runtime."""

from __future__ import annotations

import os
import sys
from typing import Mapping

REST_PROXY_URL = "http://supervisor/core"
WEBSOCKET_PROXY_URL = "ws://supervisor/core/websocket"
CONFIG_ROOT = "/homeassistant_config"
RUNTIME_UID = 999
RUNTIME_GID = 999


def build_environment(
    source_environment: Mapping[str, str],
) -> dict[str, str]:
    token = source_environment.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is required")

    environment = dict(source_environment)
    environment.update(
        {
            "HA_URL": REST_PROXY_URL,
            "HA_WEBSOCKET_URL": WEBSOCKET_PROXY_URL,
            "HA_TOKEN": token,
            "LOG_LEVEL": "INFO",
            "HA_CONFIG_READ_ENABLED": "false",
            "HA_CONFIG_ROOT": CONFIG_ROOT,
            "HA_CONFIG_REQUIRE_READ_ONLY_MOUNT": "true",
        }
    )
    environment.pop("SUPERVISOR_TOKEN", None)
    return environment


def validate_runtime_identity() -> None:
    """Fail closed unless Docker started the process as the runtime account."""
    current_uid = os.geteuid()
    current_gid = os.getegid()
    current_groups = os.getgroups()

    if current_uid == RUNTIME_UID and current_gid == RUNTIME_GID:
        return

    raise RuntimeError(
        "Refusing to start with unexpected runtime identity "
        f"uid={current_uid} gid={current_gid} groups={current_groups}; "
        f"expected {RUNTIME_UID}:{RUNTIME_GID}"
    )


def main() -> None:
    try:
        environment = build_environment(os.environ)
    except RuntimeError as exc:
        print(f"Nara Home startup error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    try:
        validate_runtime_identity()
    except RuntimeError as exc:
        print(f"Nara Home startup error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    os.execvpe("nara-home-mcp", ["nara-home-mcp"], environment)


if __name__ == "__main__":
    main()
