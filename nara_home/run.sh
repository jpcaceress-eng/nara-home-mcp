#!/usr/bin/env python3
"""Translate Home Assistant App options into Nara's fixed runtime settings."""

from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path
from typing import Mapping

OPTIONS_PATH = Path("/data/options.json")
REST_PROXY_URL = "http://supervisor/core"
WEBSOCKET_PROXY_URL = "ws://supervisor/core/websocket"
CONFIG_ROOT = "/homeassistant_config"
ALLOWED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
RUNTIME_USER = "nara"


def build_environment(
    options_path: Path,
    source_environment: Mapping[str, str],
) -> dict[str, str]:
    token = source_environment.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is required")

    try:
        options = json.loads(options_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Unable to read Home Assistant App options") from exc
    if not isinstance(options, dict):
        raise RuntimeError("Home Assistant App options must be an object")

    log_level = str(options.get("log_level", "INFO")).upper()
    if log_level not in ALLOWED_LOG_LEVELS:
        raise RuntimeError("Invalid log_level option")
    read_internal_config = options.get("read_internal_config", False)
    if not isinstance(read_internal_config, bool):
        raise RuntimeError("read_internal_config must be a boolean")

    environment = dict(source_environment)
    environment.update(
        {
            "HA_URL": REST_PROXY_URL,
            "HA_WEBSOCKET_URL": WEBSOCKET_PROXY_URL,
            "HA_TOKEN": token,
            "LOG_LEVEL": log_level,
            "HA_CONFIG_READ_ENABLED": str(read_internal_config).lower(),
            "HA_CONFIG_ROOT": CONFIG_ROOT,
            "HA_CONFIG_REQUIRE_READ_ONLY_MOUNT": "true",
        }
    )
    environment.pop("SUPERVISOR_TOKEN", None)
    return environment


def drop_privileges(user: str = RUNTIME_USER) -> None:
    """Permanently become the unprivileged runtime account."""
    if os.geteuid() != 0:
        return
    account = pwd.getpwnam(user)
    os.setgroups([])
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)


def main() -> None:
    try:
        environment = build_environment(OPTIONS_PATH, os.environ)
    except RuntimeError as exc:
        print(f"Nara Home startup error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    drop_privileges()
    os.execvpe("nara-home-mcp", ["nara-home-mcp"], environment)


if __name__ == "__main__":
    main()
