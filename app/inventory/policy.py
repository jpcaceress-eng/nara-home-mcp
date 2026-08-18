from __future__ import annotations

import re
from typing import Any


ENTITY_FIELDS = frozenset(
    {
        "area_id", "config_entry_id", "device_id", "disabled_by", "entity_category",
        "entity_id", "hidden_by", "name", "original_name", "platform",
    }
)
DEVICE_FIELDS = frozenset(
    {
        "area_id", "config_entries", "config_entry_id", "disabled_by", "entry_type",
        "hw_version", "id", "manufacturer", "model", "model_id", "name",
        "name_by_user", "primary_config_entry", "sw_version", "via_device_id",
    }
)
AREA_FIELDS = frozenset({"area_id", "floor_id", "icon", "name"})
CONFIG_ENTRY_FIELDS = frozenset(
    {"disabled_by", "domain", "entry_id", "source", "state", "title"}
)
STATE_FIELDS = frozenset({"entity_id", "state"})

_SENSITIVE_KEY = re.compile(
    r"token|secret|password|credential|authorization|url|picture|latitude|longitude|"
    r"coordinate|ip_address|email|person|serial|unique_id|connection|identifier",
    re.IGNORECASE,
)
_SAFE_STATE = re.compile(r"^[a-zA-Z0-9_.:+-]{0,64}$")
_SENSITIVE_VALUE = re.compile(
    r"(?:https?|rtsp|rtsps|ws|wss)://|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|"
    r"\b(?:bearer|token|password|secret)\b",
    re.IGNORECASE,
)


def select_fields(item: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    selected: dict[str, Any] = {}
    for key in sorted(allowed):
        value = item.get(key)
        if value is None or _SENSITIVE_KEY.search(key):
            continue
        if isinstance(value, (str, int, float, bool)):
            selected[key] = _safe_scalar(value)
        elif key == "config_entries" and isinstance(value, list):
            selected[key] = sorted(str(entry) for entry in value if isinstance(entry, str))
    return selected


def _safe_scalar(value: str | int | float | bool) -> str | int | float | bool | None:
    if isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        return None
    return value


def safe_state(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not _SAFE_STATE.fullmatch(value)
        or _SENSITIVE_VALUE.search(value)
    ):
        return None
    return value
