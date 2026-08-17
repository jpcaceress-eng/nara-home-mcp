from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ..inventory.service import InventoryQueryError, InventoryService


PageLimit = Annotated[int, Field(ge=1, le=100)]
QueryText = Annotated[str, Field(max_length=200)]
OpaqueCursor = Annotated[str, Field(min_length=1, max_length=200)]
ResourceRef = Annotated[str, Field(min_length=1, max_length=300)]
ResourceType = Literal["entity", "device", "area", "integration", "service"]
DependencyType = Literal["entity", "device", "area", "integration"]


def register_inventory_tools(mcp: FastMCP, service: InventoryService) -> None:
    @mcp.tool()
    def ha_inventory_status() -> dict[str, Any]:
        """Return generation, freshness, source health, and bounded inventory counts."""
        return service.status()

    @mcp.tool()
    def ha_search_inventory(
        resource_type: ResourceType,
        query: QueryText = "",
        cursor: OpaqueCursor | None = None,
        limit: PageLimit = 50,
        kind: str | None = None,
        area_id: str | None = None,
        integration_id: str | None = None,
    ) -> dict[str, Any]:
        """Search one inventory resource type with strict filters and pagination."""
        return _safe(lambda: service.search(resource_type, query, cursor, limit, kind, area_id, integration_id))

    @mcp.tool()
    def ha_get_device(
        device_id: ResourceRef,
        cursor: OpaqueCursor | None = None,
        limit: PageLimit = 50,
    ) -> dict[str, Any]:
        """Inspect one device and its area, integrations, and paginated entities."""
        return _safe(lambda: service.get_device(device_id, cursor, limit))

    @mcp.tool()
    def ha_get_dependencies(
        resource_type: DependencyType,
        resource_ref: ResourceRef,
        direction: Literal["outgoing", "incoming"] = "outgoing",
        cursor: OpaqueCursor | None = None,
        limit: PageLimit = 50,
    ) -> dict[str, Any]:
        """Return direct navigable inventory relations in one direction."""
        return _safe(lambda: service.dependencies(resource_type, resource_ref, direction, cursor, limit))


def _safe(operation: Any) -> dict[str, Any]:
    try:
        return operation()
    except InventoryQueryError as exc:
        raise ToolError(str(exc)) from exc
