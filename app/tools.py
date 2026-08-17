from __future__ import annotations

import os
import socket
from datetime import time, timedelta
from datetime import datetime, timezone
from numbers import Number
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .clients import HomeAssistantClient, HomeAssistantError, HomeAssistantWebSocketClient
from .configuration import EntitiesConfig
from .policy import (
    NaraSecurityError,
    ensure_allowed_raw_entity,
    resolve_climate_temperature_entity,
    resolve_presence_entities,
    resolve_room_temperature_entity,
)
from .mcp_tools import (
    register_automation_diagnostics_tools,
    register_configuration_file_tools,
    register_inventory_tools,
    register_operational_data_tools,
)
from .repositories import AutomationDiagnosticsRepository
from .services import AutomationDiagnosticsService
from .inventory import InventoryScheduler, InventoryStore
from .inventory.service import InventoryService
from .configuration_files import HomeAssistantConfigProvider, HomeAssistantConfigService


def brightness_pct_to_ha(brightness_pct: int) -> int:
    if brightness_pct < 1 or brightness_pct > 100:
        raise NaraSecurityError("brightness_pct must be between 1 and 100")
    return round((brightness_pct / 100) * 255)


def _build_state_snapshot(entity_id: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state.get("state"),
        "unit": state.get("attributes", {}).get("unit_of_measurement"),
        "friendly_name": state.get("attributes", {}).get("friendly_name"),
    }


def _parse_numeric_state(value: Any) -> float | None:
    if isinstance(value, Number):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or normalized.lower() in {"unknown", "unavailable"}:
            return None
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _format_period_timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


async def _resolve_temperature_history_points(
    ha: HomeAssistantClient,
    entity_id: str,
    start_time: datetime,
    end_time: datetime,
) -> list[dict[str, Any]]:
    history = await ha.get_history_period(
        start_time,
        end_time,
        filter_entity_id=entity_id,
        minimal_response=True,
        no_attributes=True,
    )
    if not isinstance(history, list) or not history or not isinstance(history[0], list):
        raise HomeAssistantError("Unexpected Home Assistant history response")
    return history[0]


def _summarize_temperature_history(
    states: list[dict[str, Any]],
) -> tuple[float, str, float, str, float]:
    numeric_values: list[tuple[float, str]] = []
    for state in states:
        numeric_state = _parse_numeric_state(state.get("state"))
        if numeric_state is None:
            continue
        timestamp = state.get("last_changed") or state.get("last_updated") or state.get("last_reported")
        if not isinstance(timestamp, str) or not timestamp:
            continue
        numeric_values.append((numeric_state, timestamp))

    if not numeric_values:
        raise HomeAssistantError("No temperature history data available for the requested period")

    min_temperature, min_time = min(numeric_values, key=lambda item: item[0])
    max_temperature, max_time = max(numeric_values, key=lambda item: item[0])
    average_temperature = sum(value for value, _ in numeric_values) / len(numeric_values)
    return min_temperature, min_time, max_temperature, max_time, average_temperature


def _history_timestamp_to_time(value: str) -> str:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return timestamp.strftime("%H:%M")


async def _safe_get_allowed_entity_snapshot(
    ha: HomeAssistantClient,
    entities: EntitiesConfig,
    entity_id: str | None,
) -> dict[str, Any] | None:
    if not entity_id:
        return None

    try:
        safe_entity_id = ensure_allowed_raw_entity(entities, entity_id)
    except NaraSecurityError:
        return None

    try:
        state = await ha.get_state(safe_entity_id)
    except HomeAssistantError as exc:
        if "404" in str(exc):
            return None
        return {"entity_id": safe_entity_id, "error": str(exc)}

    return _build_state_snapshot(safe_entity_id, state)


async def _summarize_infra_group(
    ha: HomeAssistantClient,
    entities: EntitiesConfig,
    group_name: str,
) -> dict[str, Any]:
    group = entities.infra.get(group_name, {})
    summary: dict[str, Any] = {}
    for alias, entity_id in group.items():
        summary[alias] = await _safe_get_allowed_entity_snapshot(ha, entities, entity_id)
    return summary


async def _summarize_simple_mapping(
    ha: HomeAssistantClient,
    entities: EntitiesConfig,
    mapping: dict[str, str | None],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for alias, entity_id in mapping.items():
        summary[alias] = await _safe_get_allowed_entity_snapshot(ha, entities, entity_id)
    return summary


def _serialize_started_at(started_at: datetime | None) -> str | None:
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at.isoformat()


def register_tools(
    mcp: FastMCP,
    ha: HomeAssistantClient,
    entities: EntitiesConfig,
    *,
    started_at: datetime | None = None,
    git_commit: str | None = None,
    automation_websocket: HomeAssistantWebSocketClient | None = None,
    inventory_store: InventoryStore | None = None,
    inventory_scheduler: InventoryScheduler | None = None,
    ha_config_provider: HomeAssistantConfigProvider | None = None,
) -> None:
    @mcp.tool()
    async def ha_get_state(entity_id: str) -> dict[str, Any]:
        """Return the complete live state and all attributes for any HA entity."""
        try:
            from .mcp_tools.operational_data import _entity_id, _sanitize
            safe_entity_id = _entity_id(entity_id)
            return _sanitize(await ha.get_state(safe_entity_id))
        except HomeAssistantError as exc:
            if "404" in str(exc):
                return {"entity_id": entity_id.strip().lower(), "availability": "not_available", "write_capability": False}
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_temperature(room: str) -> dict[str, Any]:
        """Return the temperature sensor state for an allowed room or climate location."""
        try:
            try:
                resolved = resolve_climate_temperature_entity(entities, room)
            except NaraSecurityError:
                resolved = resolve_room_temperature_entity(entities, room)
            state = await ha.get_state(resolved.entity_id)
            return {
                "room": resolved.alias,
                "location": resolved.alias,
                "entity_id": resolved.entity_id,
                "state": state.get("state"),
                "unit": state.get("attributes", {}).get("unit_of_measurement"),
                "attributes": state.get("attributes", {}),
            }
        except (NaraSecurityError, HomeAssistantError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_overnight_temperature(room: str) -> dict[str, Any]:
        """Return overnight temperature stats for an allowed room or climate location."""
        try:
            try:
                resolved = resolve_climate_temperature_entity(entities, room)
            except NaraSecurityError:
                resolved = resolve_room_temperature_entity(entities, room)

            now = datetime.now().astimezone()
            period_start = datetime.combine((now - timedelta(days=1)).date(), time(23, 0), tzinfo=now.tzinfo)
            period_end = datetime.combine(now.date(), time(8, 0), tzinfo=now.tzinfo)
            states = await _resolve_temperature_history_points(ha, resolved.entity_id, period_start, period_end)
            min_temperature, min_time, max_temperature, max_time, average_temperature = _summarize_temperature_history(
                states
            )
            return {
                "room": resolved.alias,
                "period_start": _format_period_timestamp(period_start.replace(tzinfo=None)),
                "period_end": _format_period_timestamp(period_end.replace(tzinfo=None)),
                "min_temperature": min_temperature,
                "min_time": _history_timestamp_to_time(min_time),
                "max_temperature": max_temperature,
                "max_time": _history_timestamp_to_time(max_time),
                "average_temperature": round(average_temperature, 2),
            }
        except (NaraSecurityError, HomeAssistantError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_presence() -> dict[str, Any]:
        """Return a summarized presence snapshot from allowed room sensors."""
        try:
            sensors = resolve_presence_entities(entities)
            summary: dict[str, Any] = {"rooms": {}, "occupied_rooms": []}
            for sensor in sensors:
                state = await ha.get_state(sensor.entity_id)
                is_present = str(state.get("state", "")).lower() == "on"
                summary["rooms"][sensor.alias] = {
                    "entity_id": sensor.entity_id,
                    "state": state.get("state"),
                    "present": is_present,
                }
                if is_present:
                    summary["occupied_rooms"].append(sensor.alias)
            summary["anyone_home"] = bool(summary["occupied_rooms"])
            return summary
        except (NaraSecurityError, HomeAssistantError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_proxmox_summary() -> dict[str, Any]:
        """Return a read-only summary of allowlisted Proxmox sensors."""
        try:
            return {"proxmox": await _summarize_infra_group(ha, entities, "proxmox")}
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_nas_summary() -> dict[str, Any]:
        """Return a read-only summary of allowlisted NAS/Synology sensors."""
        try:
            return {"nas": await _summarize_infra_group(ha, entities, "nas")}
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_infra_summary() -> dict[str, Any]:
        """Return a read-only summary of allowlisted home infrastructure sensors."""
        try:
            return {
                "proxmox": await _summarize_infra_group(ha, entities, "proxmox"),
                "nas": await _summarize_infra_group(ha, entities, "nas"),
            }
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_climate_summary() -> dict[str, Any]:
        """Return a read-only climate summary for allowlisted climate locations."""
        try:
            summary: dict[str, Any] = {}
            temperature_values: list[dict[str, Any]] = []
            for location_name, climate_config in entities.climate.items():
                temperature_snapshot = await _safe_get_allowed_entity_snapshot(ha, entities, climate_config.temperature)
                humidity_snapshot = await _safe_get_allowed_entity_snapshot(ha, entities, climate_config.humidity)
                battery_snapshot = await _safe_get_allowed_entity_snapshot(ha, entities, climate_config.battery)
                summary[location_name] = {
                    "temperature": temperature_snapshot,
                    "humidity": humidity_snapshot,
                    "battery": battery_snapshot,
                }
                numeric_temperature = _parse_numeric_state(temperature_snapshot["state"]) if temperature_snapshot else None
                if numeric_temperature is not None:
                    temperature_values.append(
                        {
                            "location": location_name,
                            "temperature": numeric_temperature,
                            "temperature_entity_id": temperature_snapshot["entity_id"],
                        }
                    )

            hottest = max(temperature_values, key=lambda item: item["temperature"], default=None)
            coolest = min(temperature_values, key=lambda item: item["temperature"], default=None)
            return {
                "climate": summary,
                "summary": {
                    "locations_with_temperature": len(temperature_values),
                    "hottest": hottest,
                    "coolest": coolest,
                },
            }
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def ha_get_server_version() -> dict[str, Any]:
        """Return runtime metadata for diagnosing the active MCP server."""
        tools = sorted(tool.name for tool in mcp._tool_manager.list_tools())
        return {
            "server_name": mcp.name,
            "version": os.environ.get("NARA_MCP_VERSION"),
            "git_commit": git_commit,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "started_at": _serialize_started_at(started_at),
            "tool_count": len(tools),
            "tools": tools,
            "write_capability": False,
        }

    @mcp.tool()
    async def ha_get_battery_summary(low_battery_threshold: int = 25) -> dict[str, Any]:
        """Return allowlisted battery sensors with low-battery detection."""
        if low_battery_threshold < 0 or low_battery_threshold > 100:
            raise ToolError("low_battery_threshold must be between 0 and 100")

        try:
            sensors: dict[str, Any] = {}
            low_batteries: list[str] = []
            unavailable_or_unknown: list[str] = []

            for alias, entity_id in entities.batteries.items():
                snapshot = await _safe_get_allowed_entity_snapshot(ha, entities, entity_id)
                sensors[alias] = snapshot
                if snapshot is None:
                    unavailable_or_unknown.append(alias)
                    continue

                numeric_state = _parse_numeric_state(snapshot.get("state"))
                if numeric_state is None:
                    state_text = str(snapshot.get("state", "")).lower()
                    if state_text in {"unknown", "unavailable"}:
                        unavailable_or_unknown.append(alias)
                    snapshot["low_battery"] = None
                    continue

                snapshot["percentage"] = numeric_state
                snapshot["low_battery"] = numeric_state <= low_battery_threshold
                if snapshot["low_battery"]:
                    low_batteries.append(alias)

            return {
                "threshold_pct": low_battery_threshold,
                "summary": {
                    "total_batteries": len(entities.batteries),
                    "low_batteries": len(low_batteries),
                    "unavailable_or_unknown": len(unavailable_or_unknown),
                },
                "low_battery_aliases": low_batteries,
                "unavailable_or_unknown_aliases": unavailable_or_unknown,
                "batteries": sensors,
            }
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_ups_summary() -> dict[str, Any]:
        """Return a read-only summary of allowlisted UPS sensors."""
        try:
            return {"ups": await _summarize_simple_mapping(ha, entities, entities.ups)}
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def ha_get_home_health_summary(low_battery_threshold: int = 25) -> dict[str, Any]:
        """Return a combined read-only house and infrastructure health summary."""
        try:
            climate = await ha_get_climate_summary()
            batteries = await ha_get_battery_summary(low_battery_threshold)
            ups = await ha_get_ups_summary()
            infra = await ha_get_infra_summary()
            return {
                "climate": climate["climate"],
                "batteries": batteries,
                "ups": ups["ups"],
                "infra": infra,
            }
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def ha_list_allowed_entities() -> dict[str, Any]:
        """List room aliases and the full allowlist exposed by this MCP server."""
        return {
            "rooms": {alias: config.model_dump() for alias, config in entities.rooms.items()},
            "climate": {alias: config.model_dump() for alias, config in entities.climate.items()},
            "batteries": dict(entities.batteries),
            "lights": {alias: config.model_dump() for alias, config in entities.lights.items()},
            "displays": {alias: config.model_dump() for alias, config in entities.displays.items()},
            "scenes": {alias: config.model_dump() for alias, config in entities.scenes.items()},
            "infra": entities.infra,
            "ups": dict(entities.ups),
            "allowed_raw_entities": list(entities.allowed_raw_entities),
        }

    register_operational_data_tools(mcp, ha, automation_websocket)
    if inventory_store is not None:
        register_inventory_tools(mcp, InventoryService(inventory_store, inventory_scheduler))
        if ha_config_provider is not None:
            register_configuration_file_tools(
                mcp, HomeAssistantConfigService(ha_config_provider, inventory_store)
            )
    if automation_websocket is not None:
        repository = AutomationDiagnosticsRepository(ha, automation_websocket)
        register_automation_diagnostics_tools(
            mcp,
            AutomationDiagnosticsService(
                repository,
                editable_automations=set(entities.editable_automations),
            ),
        )
