from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ..configuration_files import ConfigAccessError, HomeAssistantConfigService


PageLimit = Annotated[int, Field(ge=1, le=100)]
SafePath = Annotated[str, Field(min_length=1, max_length=500)]
Cursor = Annotated[str, Field(min_length=1, max_length=500)]
SearchText = Annotated[str, Field(min_length=1, max_length=200)]


def register_configuration_file_tools(
    mcp: FastMCP, service: HomeAssistantConfigService
) -> None:
    @mcp.tool()
    def ha_list_yaml_files(
        cursor: Cursor | None = None, limit: PageLimit = 50
    ) -> dict[str, Any]:
        """List Home Assistant YAML files recursively with bounded metadata."""
        return _safe(lambda: service.list_yaml(cursor, limit))

    @mcp.tool()
    def ha_read_yaml_file(path: SafePath) -> dict[str, Any]:
        """Read one sanitized YAML file below the configured read-only root."""
        return _safe(lambda: service.read_yaml(path))

    @mcp.tool()
    def ha_search_yaml_files(
        query: SearchText, cursor: Cursor | None = None, limit: PageLimit = 50
    ) -> dict[str, Any]:
        """Search sanitized text and entity references across all bounded YAML files."""
        return _safe(lambda: service.search_yaml(query, cursor, limit))

    @mcp.tool()
    def ha_list_dashboards(
        cursor: Cursor | None = None, limit: PageLimit = 50
    ) -> dict[str, Any]:
        """List YAML and .storage Lovelace dashboards without resolving secrets."""
        return _safe(lambda: service.list_dashboards(cursor, limit))

    @mcp.tool()
    def ha_read_dashboard(dashboard_ref: SafePath) -> dict[str, Any]:
        """Read one complete sanitized YAML or .storage dashboard."""
        return _safe(lambda: service.read_dashboard(dashboard_ref))

    @mcp.tool()
    def ha_list_config_text_files(
        cursor: Cursor | None = None, limit: PageLimit = 50
    ) -> dict[str, Any]:
        """List allowed bounded text files below the configured HA root."""
        return _safe(lambda: service.list_text(cursor, limit))

    @mcp.tool()
    def ha_read_config_text_file(
        path: SafePath, cursor: Cursor | None = None, limit: PageLimit = 100
    ) -> dict[str, Any]:
        """Read sanitized lines from one allowed text file with pagination."""
        return _safe(lambda: service.read_text(path, cursor, limit))

    @mcp.tool()
    def ha_search_config_text_files(
        query: SearchText, cursor: Cursor | None = None, limit: PageLimit = 50
    ) -> dict[str, Any]:
        """Search sanitized allowed text files and relate entity references."""
        return _safe(lambda: service.search_text(query, cursor, limit))


def _safe(operation: Any) -> dict[str, Any]:
    try:
        return operation()
    except ConfigAccessError as exc:
        raise ToolError(str(exc)) from exc
