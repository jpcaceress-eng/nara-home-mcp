from __future__ import annotations

import base64
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..clients import (
    HomeAssistantClient,
    HomeAssistantError,
    HomeAssistantWebSocketClient,
    HomeAssistantWebSocketError,
)


_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_CREDENTIAL_KEYS = {
    "access_token", "api_key", "apikey", "authorization", "client_secret",
    "cookie", "credential", "credentials", "ha_token", "password",
    "refresh_token", "secret", "token",
}
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_MAX_PAGE = 1000
_FRAGMENT = timedelta(hours=24)
_SEARCH_SYNONYMS = {
    "presion": "pressure",
    "temperatura": "temperature",
    "humedad": "humidity",
}


def _entity_id(value: str) -> str:
    normalized = value.strip().lower()
    if not _ENTITY_ID.fullmatch(normalized):
        raise ValueError("invalid entity_id")
    return normalized


def _timestamp(value: str | None, *, default: datetime) -> datetime:
    if value is None:
        return default
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamps must be ISO 8601") from None
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str, kind: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        raise ValueError("invalid cursor") from None
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        raise ValueError("invalid cursor")
    return payload


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    """Preserve operational data and redact only credential-bearing material."""
    normalized_key = key.lower() if key else ""
    if normalized_key in _CREDENTIAL_KEYS or normalized_key.endswith(
        ("_token", "_password", "_secret", "_api_key")
    ):
        return "[REDACTED:credential]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item, key=key) for item in value]
    if isinstance(value, str):
        cleaned = _BEARER.sub("[REDACTED:authorization]", value)
        cleaned = _JWT.sub("[REDACTED:jwt]", cleaned)
        try:
            parsed = urlsplit(cleaned)
            if parsed.scheme in {"http", "https", "ws", "wss"} and parsed.hostname and (
                parsed.username is not None or parsed.password is not None
            ):
                host = parsed.hostname
                if parsed.port:
                    host = f"{host}:{parsed.port}"
                cleaned = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
        except ValueError:
            pass
        return cleaned
    return value


def _search_words(value: str) -> list[str]:
    normalized = "".join(
        character for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    return [_SEARCH_SYNONYMS.get(word, word) for word in normalized.split() if word]


def register_operational_data_tools(
    mcp: FastMCP,
    rest: HomeAssistantClient,
    websocket: HomeAssistantWebSocketClient | None,
) -> None:
    @mcp.tool()
    async def ha_list_states(
        query: str | None = None,
        domain: str | None = None,
        page_size: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Discover live HA entities dynamically with complete states and attributes."""
        try:
            if page_size < 1 or page_size > _MAX_PAGE:
                raise ValueError(f"page_size must be between 1 and {_MAX_PAGE}")
            offset = int(_decode_cursor(cursor, "states")["offset"]) if cursor else 0
            normalized_domain = domain.strip().lower() if domain else None
            needles = _search_words(query) if query else []
            states = []
            for raw in await rest.list_states():
                entity = str(raw.get("entity_id", ""))
                if normalized_domain and not entity.startswith(f"{normalized_domain}."):
                    continue
                searchable = " ".join(_search_words(json.dumps(raw, ensure_ascii=False)))
                if needles and not all(needle in searchable for needle in needles):
                    continue
                states.append(_sanitize(raw))
            states.sort(key=lambda item: str(item.get("entity_id", "")))
            page = states[offset : offset + page_size]
            next_offset = offset + len(page)
            return {
                "entities": page,
                "count": len(page),
                "total": len(states),
                "cursor": _cursor({"kind": "states", "offset": next_offset}) if next_offset < len(states) else None,
                "source": "/api/states",
                "dynamic": True,
                "write_capability": False,
            }
        except (HomeAssistantError, ValueError, KeyError, TypeError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_history(
        entity_ids: list[str],
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int = 500,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read complete multi-entity Recorder history through resumable 24-hour fragments."""
        try:
            if not entity_ids or len(entity_ids) > 100:
                raise ValueError("entity_ids must contain between 1 and 100 entries")
            ids = list(dict.fromkeys(_entity_id(item) for item in entity_ids))
            if page_size < 1 or page_size > _MAX_PAGE:
                raise ValueError(f"page_size must be between 1 and {_MAX_PAGE}")
            now = datetime.now(timezone.utc)
            end = _timestamp(end_time, default=now)
            start = _timestamp(start_time, default=end - timedelta(hours=24))
            offset = 0
            fragment_start = start
            if cursor:
                data = _decode_cursor(cursor, "history")
                if data.get("ids") != ids or data.get("start") != start.isoformat() or data.get("end") != end.isoformat():
                    raise ValueError("cursor does not match this history query")
                fragment_start = _timestamp(data["fragment"], default=start)
                offset = int(data["offset"])
            if start >= end or fragment_start >= end:
                raise ValueError("start_time must be before end_time")
            fragment_end = min(fragment_start + _FRAGMENT, end)
            raw = await rest.get_history_period(
                fragment_start,
                fragment_end,
                filter_entity_id=",".join(ids),
                minimal_response=False,
                no_attributes=False,
            )
            if not isinstance(raw, list):
                raise HomeAssistantError("Unexpected Home Assistant history response")
            groups: dict[str, list[dict[str, Any]]] = {entity_id: [] for entity_id in ids}
            flat: list[tuple[str, dict[str, Any]]] = []
            for group in raw:
                if not isinstance(group, list):
                    continue
                group_entity_id = next(
                    (
                        str(point.get("entity_id"))
                        for point in group
                        if isinstance(point, dict) and point.get("entity_id") in groups
                    ),
                    None,
                )
                for point in group:
                    if not isinstance(point, dict):
                        continue
                    entity_id = point.get("entity_id") or group_entity_id
                    if entity_id in groups:
                        normalized_point = dict(point)
                        normalized_point.setdefault("entity_id", entity_id)
                        flat.append((str(entity_id), _sanitize(normalized_point)))
            flat.sort(key=lambda item: (str(item[1].get("last_updated") or item[1].get("last_changed") or ""), item[0]))
            selected = flat[offset : offset + page_size]
            for entity_id, point in selected:
                groups[entity_id].append(point)
            next_cursor = None
            if offset + len(selected) < len(flat):
                next_cursor = _cursor({"kind": "history", "ids": ids, "start": start.isoformat(), "end": end.isoformat(), "fragment": fragment_start.isoformat(), "offset": offset + len(selected)})
            elif fragment_end < end:
                next_cursor = _cursor({"kind": "history", "ids": ids, "start": start.isoformat(), "end": end.isoformat(), "fragment": fragment_end.isoformat(), "offset": 0})
            live_ids = {str(item.get("entity_id")) for item in await rest.list_states()}
            availability = {
                entity_id: ("available" if groups[entity_id] else "not_recorded" if entity_id in live_ids else "not_available")
                for entity_id in ids
            }
            return {
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "fragment": {"start": fragment_start.isoformat(), "end": fragment_end.isoformat()},
                "entities": groups,
                "availability": availability,
                "availability_note": "not_recorded means Recorder returned no rows in this fragment (excluded or outside retention); not_available means the entity also has no live state.",
                "count": len(selected),
                "cursor": next_cursor,
                "complete": next_cursor is None,
                "write_capability": False,
            }
        except (HomeAssistantError, ValueError, KeyError, TypeError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_statistics(
        statistic_ids: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        period: str = "hour",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List Recorder statistic metadata or read short/long-term statistic rows."""
        if websocket is None:
            raise ToolError("Recorder statistics transport is not available")
        try:
            metadata = await websocket.get_statistics_metadata(statistic_ids)
            if not statistic_ids:
                return {"metadata": _sanitize(metadata), "write_capability": False}
            if period not in {"5minute", "hour", "day", "week", "month", "year"}:
                raise ValueError("invalid statistics period")
            now = datetime.now(timezone.utc)
            end = _timestamp(end_time, default=now)
            start = _timestamp(start_time, default=end - timedelta(hours=24))
            fragment_start = start
            if cursor:
                data = _decode_cursor(cursor, "statistics")
                if data.get("ids") != statistic_ids or data.get("start") != start.isoformat() or data.get("end") != end.isoformat() or data.get("period") != period:
                    raise ValueError("cursor does not match this statistics query")
                fragment_start = _timestamp(data["fragment"], default=start)
            fragment_span = timedelta(days=7 if period == "5minute" else 90)
            fragment_end = min(fragment_start + fragment_span, end)
            rows = await websocket.get_statistics_during_period(statistic_ids, fragment_start.isoformat(), fragment_end.isoformat(), period)
            next_cursor = _cursor({"kind": "statistics", "ids": statistic_ids, "start": start.isoformat(), "end": end.isoformat(), "period": period, "fragment": fragment_end.isoformat()}) if fragment_end < end else None
            return {"metadata": _sanitize(metadata), "statistics": _sanitize(rows), "period": period, "fragment": {"start": fragment_start.isoformat(), "end": fragment_end.isoformat()}, "cursor": next_cursor, "complete": next_cursor is None, "write_capability": False}
        except (HomeAssistantError, HomeAssistantWebSocketError, ValueError, KeyError, TypeError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_logbook(
        start_time: str | None = None,
        end_time: str | None = None,
        entity_id: str | None = None,
        page_size: int = 500,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read Logbook events for any retained period and optional entity."""
        try:
            now = datetime.now(timezone.utc)
            end = _timestamp(end_time, default=now)
            start = _timestamp(start_time, default=end - timedelta(hours=24))
            safe_id = _entity_id(entity_id) if entity_id else None
            if page_size < 1 or page_size > _MAX_PAGE:
                raise ValueError(f"page_size must be between 1 and {_MAX_PAGE}")
            if start >= end:
                raise ValueError("start_time must be before end_time")
            fragment_start = start
            offset = 0
            if cursor:
                data = _decode_cursor(cursor, "logbook")
                if data.get("entity_id") != safe_id or data.get("start") != start.isoformat() or data.get("end") != end.isoformat():
                    raise ValueError("cursor does not match this Logbook query")
                fragment_start = _timestamp(data["fragment"], default=start)
                offset = int(data["offset"])
            fragment_end = min(fragment_start + _FRAGMENT, end)
            events = await rest.get_logbook_period(fragment_start, fragment_end, entity_id=safe_id)
            if not isinstance(events, list):
                raise HomeAssistantError("Unexpected Home Assistant Logbook response")
            selected = events[offset : offset + page_size]
            next_cursor = None
            if offset + len(selected) < len(events):
                next_cursor = _cursor({"kind": "logbook", "entity_id": safe_id, "start": start.isoformat(), "end": end.isoformat(), "fragment": fragment_start.isoformat(), "offset": offset + len(selected)})
            elif fragment_end < end:
                next_cursor = _cursor({"kind": "logbook", "entity_id": safe_id, "start": start.isoformat(), "end": end.isoformat(), "fragment": fragment_end.isoformat(), "offset": 0})
            return {"period": {"start": start.isoformat(), "end": end.isoformat()}, "fragment": {"start": fragment_start.isoformat(), "end": fragment_end.isoformat()}, "entity_id": safe_id, "events": _sanitize(selected), "count": len(selected), "cursor": next_cursor, "complete": next_cursor is None, "write_capability": False}
        except (HomeAssistantError, ValueError, KeyError, TypeError) as exc:
            raise ToolError(str(exc)) from exc
