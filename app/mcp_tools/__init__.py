"""MCP tool adapters."""

from .automations import register_automation_diagnostics_tools
from .configuration_files import register_configuration_file_tools
from .history import register_history_tool, register_recent_changes_tool
from .inventory import register_inventory_tools
from .operational_data import register_operational_data_tools

__all__ = [
    "register_automation_diagnostics_tools",
    "register_configuration_file_tools",
    "register_history_tool",
    "register_inventory_tools",
    "register_operational_data_tools",
    "register_recent_changes_tool",
]
