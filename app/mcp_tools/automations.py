from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from ..clients import HomeAssistantError, HomeAssistantWebSocketError
from ..devtools import CaptureAuditError
from ..services import AutomationDiagnosticsError, AutomationDiagnosticsService


AutomationRef = Annotated[str, Field(pattern=r"^automation_[0-9]{3,}$")]
AutomationEntityId = Annotated[str, Field(pattern=r"^automation\.[a-z0-9_]+$")]
RunRef = Annotated[str, Field(pattern=r"^run_[0-9]{3,}$")]
TraceLimit = Annotated[int, Field(ge=1, le=20)]
SearchText = Annotated[str, Field(min_length=1, max_length=200)]
SearchResultLimit = Annotated[int, Field(ge=1, le=50)]
UsageResultLimit = Annotated[int, Field(ge=1, le=100)]
MatchLimit = Annotated[int, Field(ge=1, le=50)]
DetailedResultLimit = Annotated[int, Field(ge=1, le=200)]
HealthResultLimit = Annotated[int, Field(ge=1, le=500)]
ProposalRef = Annotated[str, Field(pattern=r"^proposal_[a-f0-9]{24}$")]
UsageQuery = Annotated[
    str,
    Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$", min_length=3, max_length=200),
]


class AutomationEntityReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurrence_ref: Annotated[str, Field(pattern=r"^occ_[a-f0-9]{16}$")]
    replacement_entity_id: Annotated[
        str,
        Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$", max_length=200),
    ]


AutomationReplacements = Annotated[
    list[AutomationEntityReplacement],
    Field(min_length=1, max_length=10),
]


def register_automation_diagnostics_tools(
    mcp: FastMCP,
    service: AutomationDiagnosticsService,
) -> None:
    """Register the bounded, read-only automation diagnostic API."""

    @mcp.tool()
    async def ha_list_automations() -> dict[str, Any]:
        """List automations using opaque references and anonymized metadata."""
        return await _safe(service.list_automations())

    @mcp.tool()
    async def ha_get_automation_config(automation_ref: AutomationRef) -> dict[str, Any]:
        """Return an anonymized configuration for an opaque automation reference."""
        return await _safe(service.get_automation_config(automation_ref))

    @mcp.tool()
    async def ha_get_automation_yaml(
        automation_ref: AutomationRef | None = None,
        entity_id: AutomationEntityId | None = None,
    ) -> dict[str, Any]:
        """Return structured and YAML forms of one safely sanitized automation."""
        return await _safe(service.get_automation_yaml(automation_ref, entity_id))

    @mcp.tool()
    async def ha_search_automations(
        query: SearchText,
        max_results: SearchResultLimit = 25,
        max_matches_per_automation: MatchLimit = 20,
    ) -> dict[str, Any]:
        """Search sanitized automation structure and return exact matching paths."""
        return await _safe(
            service.search_automations(query, max_results, max_matches_per_automation)
        )

    @mcp.tool()
    async def ha_find_entity_usage(
        query: UsageQuery,
        max_results: UsageResultLimit = 50,
        max_matches_per_automation: MatchLimit = 50,
    ) -> dict[str, Any]:
        """Find exact entity or service usage in sanitized automations."""
        return await _safe(
            service.find_entity_usage(query, max_results, max_matches_per_automation)
        )

    @mcp.tool()
    async def ha_list_automations_detailed(
        max_results: DetailedResultLimit = 100,
    ) -> dict[str, Any]:
        """List bounded automation state and trigger/action summaries."""
        return await _safe(service.list_automations_detailed(max_results))

    @mcp.tool()
    async def ha_scan_entity_health(
        max_results: HealthResultLimit = 200,
    ) -> dict[str, Any]:
        """List unhealthy Home Assistant entities with exact technical identifiers."""
        return await _safe(service.scan_entity_health(max_results))

    @mcp.tool()
    async def ha_find_broken_automation_references(
        automation_ref: AutomationRef | None = None,
        max_results: HealthResultLimit = 200,
    ) -> dict[str, Any]:
        """Find broken structured references and possible references in templates."""
        return await _safe(
            service.find_broken_automation_references(automation_ref, max_results)
        )

    @mcp.tool()
    async def ha_analyze_automation(
        automation_ref: AutomationRef,
        max_results: HealthResultLimit = 200,
    ) -> dict[str, Any]:
        """Analyze one automation for unhealthy entity and service references."""
        return await _safe(service.analyze_automation(automation_ref, max_results))

    @mcp.tool()
    async def ha_prepare_automation_edit(
        automation_ref: AutomationRef,
        replacements: AutomationReplacements,
    ) -> dict[str, Any]:
        """Build an expiring in-memory diff; this tool cannot write to Home Assistant."""
        return await _safe(
            service.prepare_automation_edit(
                automation_ref,
                [replacement.model_dump() for replacement in replacements],
            )
        )

    @mcp.tool()
    async def ha_get_automation_edit_proposal(
        proposal_ref: ProposalRef,
    ) -> dict[str, Any]:
        """Return one active read-only automation edit proposal and its exact diff."""
        return await _safe(service.get_automation_edit_proposal(proposal_ref))

    @mcp.tool()
    async def ha_list_automation_traces(
        automation_ref: AutomationRef,
        max_traces: TraceLimit = 10,
    ) -> dict[str, Any]:
        """List bounded, anonymized trace summaries for an automation."""
        return await _safe(service.list_automation_traces(automation_ref, max_traces))

    @mcp.tool()
    async def ha_get_automation_trace(
        automation_ref: AutomationRef,
        run_ref: RunRef,
    ) -> dict[str, Any]:
        """Return one anonymized execution trace selected by opaque references."""
        return await _safe(service.get_automation_trace(automation_ref, run_ref))

    @mcp.tool()
    async def ha_diagnose_automation_trace(
        automation_ref: AutomationRef,
        run_ref: RunRef,
    ) -> dict[str, Any]:
        """Return deterministic evidence and a human summary for one execution."""
        return await _safe(service.diagnose_automation_trace(automation_ref, run_ref))


async def _safe(operation: Any) -> dict[str, Any]:
    try:
        return await operation
    except (
        AutomationDiagnosticsError,
        CaptureAuditError,
        HomeAssistantError,
        HomeAssistantWebSocketError,
    ) as exc:
        raise ToolError(str(exc)) from exc
