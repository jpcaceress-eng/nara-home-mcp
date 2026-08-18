from __future__ import annotations

import json
import asyncio
import copy
import hashlib
import re
import secrets
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

import yaml

from ..devtools import AnonymizationLimits, audit_serialized_capture
from ..repositories.automations import AutomationDiagnosticsRepository


MAX_AUTOMATIONS = 200
MAX_TRACES = 20
MAX_ENTITY_CATALOG = 5_000
MAX_HEALTH_RESULTS = 500
MAX_ANALYSES_PER_MINUTE = 30
MAX_ACTIVE_PROPOSALS = 50
MAX_REPLACEMENTS_PER_PROPOSAL = 10
PROPOSAL_TTL_SECONDS = 600.0
INVENTORY_TTL_SECONDS = 45.0
INVENTORY_CONCURRENCY = 5
PUBLIC_ANONYMIZATION_LIMITS = AnonymizationLimits(
    max_depth=16,
    max_string_length=1_024,
    max_collection_items=200,
    max_total_bytes=512_000,
)


class AutomationDiagnosticsError(ValueError):
    """A safe public error from read-only automation diagnostics."""


class AutomationDiagnosticsService:
    """Expose bounded automation diagnostics through opaque in-memory references."""

    def __init__(
        self,
        repository: AutomationDiagnosticsRepository,
        *,
        editable_automations: set[str] | None = None,
    ) -> None:
        self._repository = repository
        self._editable_automations = frozenset(editable_automations or set())
        self._proposal_policy_digest = _digest_value(sorted(self._editable_automations))
        self._automation_by_ref: dict[str, str] = {}
        self._automation_ref_by_entity: dict[str, str] = {}
        self._run_by_ref: dict[tuple[str, str], str] = {}
        self._run_ref_by_id: dict[tuple[str, str], str] = {}
        self._inventory: list[dict[str, Any]] | None = None
        self._inventory_errors: list[dict[str, str]] = []
        self._inventory_created_at = 0.0
        self._inventory_generation = 0
        self._inventory_lock = asyncio.Lock()
        self._health_catalog: dict[str, Any] | None = None
        self._health_catalog_created_at = 0.0
        self._health_catalog_lock = asyncio.Lock()
        self._analysis_semaphore = asyncio.Semaphore(2)
        self._analysis_rate_lock = asyncio.Lock()
        self._analysis_started_at: deque[float] = deque()
        self._occurrence_refs: dict[tuple[str, str, str], str] = {}
        self._occurrence_by_ref: dict[str, dict[str, Any]] = {}
        self._proposals: dict[str, dict[str, Any]] = {}
        self._proposal_lock = asyncio.Lock()

    def invalidate_inventory(self) -> None:
        """Invalidate the private automation inventory without exposing an MCP tool."""
        self._inventory = None
        self._inventory_errors = []
        self._inventory_created_at = 0.0
        self._health_catalog = None
        self._health_catalog_created_at = 0.0
        self._occurrence_refs.clear()
        self._occurrence_by_ref.clear()

    async def get_automation_yaml(
        self, automation_ref: str | None, entity_id: str | None
    ) -> dict[str, Any]:
        item = await self._select_inventory_item(automation_ref, entity_id)
        sanitizer = AutomationConfigSanitizer(PUBLIC_ANONYMIZATION_LIMITS)
        config = sanitizer.sanitize(item["config"])
        rendered = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        yaml_truncated = len(rendered.encode("utf-8")) > PUBLIC_ANONYMIZATION_LIMITS.max_total_bytes
        if yaml_truncated:
            rendered = "[TRUNCATED:total_size]"
        payload = {
            "automation_ref": item["automation_ref"],
            "entity_id": item["entity_id"],
            "alias": _safe_alias(item),
            "config": config,
            "yaml": rendered,
            "redactions": dict(sanitizer.redactions),
            "truncated": sanitizer.truncated or yaml_truncated,
            **self._inventory_status(),
        }
        self._audit(payload, [_internal_id_from_config(item["config"])], allow_raw_entity_ids=True)
        return payload

    async def search_automations(
        self, query: str, max_results: int, max_matches_per_automation: int
    ) -> dict[str, Any]:
        needle = query.casefold()
        results: list[dict[str, Any]] = []
        total_matches = 0
        matches_truncated = False
        for item in await self._get_inventory():
            sanitized = AutomationConfigSanitizer(PUBLIC_ANONYMIZATION_LIMITS).sanitize(item["config"])
            all_matches = _search_structure(sanitized, needle)
            matches = all_matches[:max_matches_per_automation]
            if not matches:
                continue
            total_matches += len(all_matches)
            item_truncated = len(all_matches) > len(matches)
            matches_truncated = matches_truncated or item_truncated
            results.append(
                _result_identity(item)
                | {"matches": matches, "matches_truncated": item_truncated}
            )
        available = len(results)
        results = results[:max_results]
        payload = {
            "query": query,
            "count": len(results),
            "available_before_limit": available,
            "match_count": total_matches,
            "max_results": max_results,
            "max_matches_per_automation": max_matches_per_automation,
            "truncated": available > len(results) or matches_truncated,
            "results": results,
            **self._inventory_status(),
        }
        self._audit(payload, [], allow_raw_entity_ids=True)
        return payload

    async def find_entity_usage(
        self, query: str, max_results: int, max_matches_per_automation: int
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        total_usages = 0
        usages_truncated = False
        for item in await self._get_inventory():
            sanitized = AutomationConfigSanitizer(PUBLIC_ANONYMIZATION_LIMITS).sanitize(item["config"])
            all_usages = _find_exact_usage(sanitized, query)
            usages = all_usages[:max_matches_per_automation]
            if not usages:
                continue
            total_usages += len(all_usages)
            item_truncated = len(all_usages) > len(usages)
            usages_truncated = usages_truncated or item_truncated
            results.append(
                _result_identity(item)
                | {"usages": usages, "usages_truncated": item_truncated}
            )
        available = len(results)
        results = results[:max_results]
        payload = {
            "query": query,
            "count": len(results),
            "available_before_limit": available,
            "usage_count": total_usages,
            "max_results": max_results,
            "max_matches_per_automation": max_matches_per_automation,
            "truncated": available > len(results) or usages_truncated,
            "results": results,
            **self._inventory_status(),
        }
        self._audit(payload, [], allow_raw_entity_ids=True)
        return payload

    async def list_automations_detailed(self, max_results: int) -> dict[str, Any]:
        inventory = await self._get_inventory()
        details = []
        for item in inventory[:max_results]:
            sanitizer = AutomationConfigSanitizer(PUBLIC_ANONYMIZATION_LIMITS)
            config = sanitizer.sanitize(item["config"])
            details.append(
                _result_identity(item)
                | {
                    "state": _safe_state(item["state"].get("state")),
                    "last_triggered": _safe_timestamp(item["attributes"].get("last_triggered")),
                    "mode": config.get("mode") if isinstance(config, dict) else None,
                    "triggers": _summarize_steps(config, "trigger"),
                    "actions": _summarize_steps(config, "action"),
                }
            )
        payload = {
            "count": len(details),
            "available_before_limit": len(inventory),
            "max_results": max_results,
            "truncated": len(inventory) > len(details),
            "automations": details,
            **self._inventory_status(),
        }
        self._audit(payload, [], allow_raw_entity_ids=True)
        return payload

    async def scan_entity_health(self, max_results: int) -> dict[str, Any]:
        _ensure_health_limit(max_results)
        await self._enforce_analysis_rate_limit()
        async with self._analysis_semaphore:
            catalog = await self._get_health_catalog()
            results: list[dict[str, Any]] = []
            counts: Counter[str] = Counter()
            for entity_id in sorted(set(catalog["states"]) | set(catalog["registry"])):
                status = _entity_health_status(entity_id, catalog)
                counts[status] += 1
                if status == "healthy":
                    continue
                state = catalog["states"].get(entity_id, {})
                registry = catalog["registry"].get(entity_id, {})
                attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
                friendly_name = (
                    attributes.get("friendly_name")
                    or registry.get("name")
                    or registry.get("original_name")
                )
                results.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": _sanitize_public_text(friendly_name),
                        "status": status,
                    }
                )
            available = len(results)
            payload = {
                "count": min(available, max_results),
                "available_before_limit": available,
                "max_results": max_results,
                "truncated": available > max_results or catalog["truncated"],
                "summary": dict(sorted(counts.items())),
                "entities": results[:max_results],
                "partial": bool(catalog["errors"]),
                "errors": list(catalog["errors"]),
            }
            self._audit(payload, [], allow_raw_entity_ids=True)
            return payload

    async def find_broken_automation_references(
        self, automation_ref: str | None, max_results: int
    ) -> dict[str, Any]:
        _ensure_health_limit(max_results)
        await self._enforce_analysis_rate_limit()
        async with self._analysis_semaphore:
            inventory = await self._get_inventory()
            if automation_ref is not None:
                inventory = [await self._select_inventory_item(automation_ref, None)]
            catalog = await self._get_health_catalog()
            findings: list[dict[str, Any]] = []
            for item in inventory:
                findings.extend(self._broken_references_for_item(item, catalog))
            available = len(findings)
            payload = {
                "automation_ref": automation_ref,
                "count": min(available, max_results),
                "available_before_limit": available,
                "max_results": max_results,
                "truncated": available > max_results,
                "findings": findings[:max_results],
                "partial": bool(self._inventory_errors or catalog["errors"]),
                "errors": [*self._inventory_errors, *catalog["errors"]],
            }
            self._audit(payload, [], allow_raw_entity_ids=True)
            return payload

    async def analyze_automation(
        self, automation_ref: str, max_results: int
    ) -> dict[str, Any]:
        _ensure_health_limit(max_results)
        await self._enforce_analysis_rate_limit()
        async with self._analysis_semaphore:
            item = await self._select_inventory_item(automation_ref, None)
            catalog = await self._get_health_catalog()
            findings = self._broken_references_for_item(item, catalog)
            editable_occurrences = self._editable_occurrences_for_item(item, catalog)
            available = len(findings)
            counts = Counter(finding["status"] for finding in findings)
            payload = {
                **_result_identity(item),
                "edit_proposal_eligible": item["entity_id"] in self._editable_automations,
                "healthy": not findings and not catalog["errors"],
                "summary": dict(sorted(counts.items())),
                "count": min(available, max_results),
                "available_before_limit": available,
                "max_results": max_results,
                "truncated": available > max_results,
                "findings": findings[:max_results],
                "editable_occurrences": editable_occurrences,
                "partial": bool(catalog["errors"]),
                "errors": list(catalog["errors"]),
            }
            self._audit(payload, [], allow_raw_entity_ids=True)
            return payload

    async def prepare_automation_edit(
        self,
        automation_ref: str,
        replacements: list[dict[str, str]],
    ) -> dict[str, Any]:
        if not replacements or len(replacements) > MAX_REPLACEMENTS_PER_PROPOSAL:
            raise AutomationDiagnosticsError(
                f"replacements must contain between 1 and {MAX_REPLACEMENTS_PER_PROPOSAL} items"
            )
        item = await self._select_inventory_item(automation_ref, None)
        if item["entity_id"] not in self._editable_automations:
            raise AutomationDiagnosticsError("Automation is not allowlisted for edit proposals")

        current_response = await self._repository.get_config(item["entity_id"])
        current_config = _unwrap_config(current_response)
        inventory_digest = _config_digest(item["config"])
        current_digest = _config_digest(current_config)
        if current_digest != inventory_digest:
            self.invalidate_inventory()
            raise AutomationDiagnosticsError(
                "Automation configuration changed; analyze it again before proposing an edit"
            )

        catalog = await self._get_health_catalog()
        candidate = copy.deepcopy(current_config)
        diff: list[dict[str, str]] = []
        seen_occurrences: set[str] = set()
        for replacement in replacements:
            occurrence_ref = replacement.get("occurrence_ref", "")
            replacement_entity_id = replacement.get("replacement_entity_id", "")
            if occurrence_ref in seen_occurrences:
                raise AutomationDiagnosticsError("Each occurrence_ref may appear only once")
            seen_occurrences.add(occurrence_ref)
            occurrence = self._occurrence_by_ref.get(occurrence_ref)
            if occurrence is None or occurrence["automation_ref"] != automation_ref:
                raise AutomationDiagnosticsError(
                    "Unknown occurrence reference; analyze the automation again"
                )
            if occurrence["inventory_generation"] != self._inventory_generation:
                raise AutomationDiagnosticsError(
                    "Unknown occurrence reference; analyze the automation again"
                )
            if occurrence["automation_entity_id"] != item["entity_id"]:
                raise AutomationDiagnosticsError(
                    "Occurrence reference belongs to a different automation"
                )
            if occurrence["kind"] != "entity":
                raise AutomationDiagnosticsError(
                    "Only structured entity references can be proposed for replacement"
                )
            if not _ENTITY_ID_FULL_RE.fullmatch(replacement_entity_id):
                raise AutomationDiagnosticsError("Invalid replacement entity_id")
            if _entity_health_status(replacement_entity_id, catalog) != "healthy":
                raise AutomationDiagnosticsError(
                    "Replacement entity must exist and be healthy"
                )
            if _entity_domain(occurrence["value"]) != _entity_domain(replacement_entity_id):
                raise AutomationDiagnosticsError(
                    "Replacement entity domain must match the original entity domain"
                )
            actual_value = _value_at_parts(candidate, occurrence["parts"])
            if actual_value != occurrence["value"]:
                self.invalidate_inventory()
                raise AutomationDiagnosticsError(
                    "Automation occurrence changed; analyze it again before proposing an edit"
                )
            _set_at_parts(candidate, occurrence["parts"], replacement_entity_id)
            diff.append(
                {
                    "occurrence_ref": occurrence_ref,
                    "path": occurrence["path"],
                    "before_entity_id": occurrence["value"],
                    "after_entity_id": replacement_entity_id,
                }
            )

        candidate_digest = _config_digest(candidate)
        proposal_ref = f"proposal_{secrets.token_hex(12)}"
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=PROPOSAL_TTL_SECONDS)
        digest_source = {
            "proposal_ref": proposal_ref,
            "automation_ref": automation_ref,
            "base_digest": current_digest,
            "candidate_digest": candidate_digest,
            "diff": diff,
        }
        proposal_digest = _digest_value(digest_source)
        payload = {
            "proposal_ref": proposal_ref,
            **_result_identity(item),
            "operation": "replace_structured_entity_references",
            "base_digest": current_digest,
            "candidate_digest": candidate_digest,
            "proposal_digest": proposal_digest,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "replacement_count": len(diff),
            "diff": diff,
            "confirmation": {
                "required_for_future_apply": True,
                "phrase": f"APPLY {proposal_ref} {proposal_digest[-12:]}",
            },
            "write_capability": False,
        }
        async with self._proposal_lock:
            self._discard_expired_proposals()
            if len(self._proposals) >= MAX_ACTIVE_PROPOSALS:
                raise AutomationDiagnosticsError("Too many active edit proposals")
            self._proposals[proposal_ref] = {
                "expires_monotonic": monotonic() + PROPOSAL_TTL_SECONDS,
                "payload": copy.deepcopy(payload),
                "automation_entity_id": item["entity_id"],
                "base_digest": current_digest,
                "replacement_entity_ids": tuple(
                    replacement["replacement_entity_id"] for replacement in replacements
                ),
                "policy_digest": self._proposal_policy_digest,
            }
        self._audit(payload, [], allow_raw_entity_ids=True)
        return payload

    async def get_automation_edit_proposal(self, proposal_ref: str) -> dict[str, Any]:
        async with self._proposal_lock:
            self._discard_expired_proposals()
            proposal = self._proposals.get(proposal_ref)
            if proposal is None:
                raise AutomationDiagnosticsError("Unknown or expired edit proposal")
            invalid_reason = await self._proposal_invalid_reason(proposal)
            if invalid_reason is not None:
                self._proposals.pop(proposal_ref, None)
                raise AutomationDiagnosticsError(invalid_reason)
            payload = copy.deepcopy(proposal["payload"])
        self._audit(payload, [], allow_raw_entity_ids=True)
        return payload

    async def _proposal_invalid_reason(self, proposal: dict[str, Any]) -> str | None:
        if proposal.get("policy_digest") != self._proposal_policy_digest:
            return "Proposal authorization policy changed; analyze the automation again"
        try:
            current = _unwrap_config(
                await self._repository.get_config(proposal["automation_entity_id"])
            )
        except Exception:
            return "Proposal source is unavailable; analyze the automation again"
        if _config_digest(current) != proposal.get("base_digest"):
            return "Automation configuration changed; analyze it again before proposing an edit"
        try:
            states = {
                item.get("entity_id"): item
                for item in await self._repository.list_states()
                if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
            }
            registry = {
                item.get("entity_id"): item
                for item in await self._repository.list_entity_registry()
                if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
            }
        except Exception:
            return "Proposal destination changed or is unavailable; analyze the automation again"
        catalog = {
            "states": states,
            "registry": registry,
            "states_available": True,
            "registry_available": True,
        }
        for entity_id in proposal.get("replacement_entity_ids", ()):
            if _entity_health_status(entity_id, catalog) != "healthy":
                return "Proposal destination changed or is unavailable; analyze the automation again"
        return None

    def _discard_expired_proposals(self) -> None:
        now = monotonic()
        expired = [
            proposal_ref
            for proposal_ref, proposal in self._proposals.items()
            if proposal["expires_monotonic"] <= now
        ]
        for proposal_ref in expired:
            self._proposals.pop(proposal_ref, None)

    async def _enforce_analysis_rate_limit(self) -> None:
        now = monotonic()
        async with self._analysis_rate_lock:
            while self._analysis_started_at and now - self._analysis_started_at[0] >= 60.0:
                self._analysis_started_at.popleft()
            if len(self._analysis_started_at) >= MAX_ANALYSES_PER_MINUTE:
                raise AutomationDiagnosticsError(
                    "Automation analysis rate limit exceeded; retry later"
                )
            self._analysis_started_at.append(now)

    def _broken_references_for_item(
        self, item: dict[str, Any], catalog: dict[str, Any]
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for reference in _collect_structured_references(item["config"]):
            value = reference["value"]
            kind = reference["kind"]
            if kind == "service":
                if not catalog["services_available"] or value.casefold() in catalog["services"]:
                    continue
                status = "service_missing"
            else:
                entity_status = _entity_health_status(value, catalog)
                if entity_status == "healthy":
                    continue
                status = "possible_template_reference" if kind == "template" else entity_status
            finding = {
                    **_result_identity(item),
                    "path": reference["path"],
                    "kind": kind,
                    "entity_id" if kind != "service" else "service": value,
                    "status": status,
                    **({"referenced_status": entity_status} if kind == "template" else {}),
                }
            if kind == "entity" and item["entity_id"] in self._editable_automations:
                occurrence = self._register_editable_occurrence(item, reference, entity_status)
                finding.update(occurrence)
            findings.append(finding)
        return findings

    def _editable_occurrences_for_item(
        self, item: dict[str, Any], catalog: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if item["entity_id"] not in self._editable_automations:
            return []
        return [
            self._register_editable_occurrence(
                item,
                reference,
                _entity_health_status(reference["value"], catalog),
            )
            for reference in _collect_structured_references(item["config"])
            if reference["kind"] == "entity"
        ]

    def _register_editable_occurrence(
        self,
        item: dict[str, Any],
        reference: dict[str, Any],
        health_status: str,
    ) -> dict[str, Any]:
        key = (item["automation_ref"], reference["path"], reference["value"])
        occurrence_ref = self._occurrence_refs.get(key)
        if occurrence_ref is None:
            occurrence_ref = f"occ_{secrets.token_hex(8)}"
            self._occurrence_refs[key] = occurrence_ref
        stored = {
            "occurrence_ref": occurrence_ref,
            "automation_ref": item["automation_ref"],
            "automation_entity_id": item["entity_id"],
            "inventory_generation": self._inventory_generation,
            "path": reference["path"],
            "parts": tuple(reference["parts"]),
            "kind": "entity",
            "value": reference["value"],
            "health_status": health_status,
        }
        self._occurrence_by_ref[occurrence_ref] = stored
        return {
            "occurrence_ref": occurrence_ref,
            "automation_ref": item["automation_ref"],
            "automation_entity_id": item["entity_id"],
            "inventory_generation": self._inventory_generation,
            "path": reference["path"],
            "kind": "entity",
            "current_entity_id": reference["value"],
            "health_status": health_status,
        }

    async def _get_health_catalog(self) -> dict[str, Any]:
        if self._health_catalog is not None and monotonic() - self._health_catalog_created_at < INVENTORY_TTL_SECONDS:
            return self._health_catalog
        async with self._health_catalog_lock:
            if self._health_catalog is not None and monotonic() - self._health_catalog_created_at < INVENTORY_TTL_SECONDS:
                return self._health_catalog
            errors: list[dict[str, str]] = []
            states: dict[str, dict[str, Any]] = {}
            registry: dict[str, dict[str, Any]] = {}
            services: set[str] = set()
            truncated = False
            states_available = True
            try:
                raw_states = await self._repository.list_states()
                truncated = len(raw_states) > MAX_ENTITY_CATALOG
                for state in raw_states[:MAX_ENTITY_CATALOG]:
                    entity_id = state.get("entity_id") if isinstance(state, dict) else None
                    if isinstance(entity_id, str) and _ENTITY_ID_FULL_RE.fullmatch(entity_id):
                        states[entity_id] = state
            except Exception:
                states_available = False
                errors.append(_catalog_error("states_unavailable", "Entity states are temporarily unavailable."))
            registry_available = True
            try:
                raw_registry = await self._repository.list_entity_registry()
                if not isinstance(raw_registry, list):
                    raise ValueError("invalid registry")
                truncated = truncated or len(raw_registry) > MAX_ENTITY_CATALOG
                for entry in raw_registry[:MAX_ENTITY_CATALOG]:
                    entity_id = entry.get("entity_id") if isinstance(entry, dict) else None
                    if isinstance(entity_id, str) and _ENTITY_ID_FULL_RE.fullmatch(entity_id):
                        registry[entity_id] = entry
            except Exception:
                registry_available = False
                errors.append(_catalog_error("entity_registry_unavailable", "Entity registry is temporarily unavailable."))
            services_available = True
            try:
                raw_services = await self._repository.list_services()
                services = _service_catalog(raw_services)
            except Exception:
                services_available = False
                errors.append(_catalog_error("services_unavailable", "Service catalog is temporarily unavailable."))
            self._health_catalog = {
                "states": states,
                "states_available": states_available,
                "registry": registry,
                "registry_available": registry_available,
                "services": services,
                "services_available": services_available,
                "errors": errors,
                "truncated": truncated,
            }
            self._health_catalog_created_at = monotonic()
            return self._health_catalog

    async def _select_inventory_item(
        self, automation_ref: str | None, entity_id: str | None
    ) -> dict[str, Any]:
        if (automation_ref is None) == (entity_id is None):
            raise AutomationDiagnosticsError("Provide exactly one of automation_ref or entity_id")
        inventory = await self._get_inventory()
        for item in inventory:
            if automation_ref == item["automation_ref"] or entity_id == item["entity_id"]:
                return item
        if automation_ref is not None:
            raise AutomationDiagnosticsError("Unknown automation reference; call ha_list_automations first")
        raise AutomationDiagnosticsError("Unknown automation entity_id")

    async def _get_inventory(self) -> list[dict[str, Any]]:
        if self._inventory is not None and monotonic() - self._inventory_created_at < INVENTORY_TTL_SECONDS:
            return self._inventory
        async with self._inventory_lock:
            if self._inventory is not None and monotonic() - self._inventory_created_at < INVENTORY_TTL_SECONDS:
                return self._inventory
            self._occurrence_refs.clear()
            self._occurrence_by_ref.clear()
            states = sorted(
                await self._repository.list_automation_states(),
                key=lambda state: str(state.get("entity_id", "")),
            )[:MAX_AUTOMATIONS]
            semaphore = asyncio.Semaphore(INVENTORY_CONCURRENCY)

            async def load(state: dict[str, Any]) -> dict[str, Any] | None:
                entity_id = state.get("entity_id")
                if not isinstance(entity_id, str):
                    return None
                automation_ref = self._automation_ref(entity_id)
                async with semaphore:
                    try:
                        config = await self._repository.get_config(entity_id)
                    except Exception:
                        return {
                            "__error__": True,
                            "automation_ref": automation_ref,
                            "entity_id": entity_id,
                            "error_code": "config_unavailable",
                            "message": "Automation configuration is temporarily unavailable.",
                        }
                return {
                    "automation_ref": automation_ref,
                    "entity_id": entity_id,
                    "state": state,
                    "attributes": state.get("attributes") if isinstance(state.get("attributes"), dict) else {},
                    "config": _unwrap_config(config),
                }

            loaded = await asyncio.gather(*(load(state) for state in states))
            self._inventory_errors = [
                {
                    "automation_ref": str(item["automation_ref"]),
                    "entity_id": str(item["entity_id"]),
                    "error_code": str(item["error_code"]),
                    "message": str(item["message"]),
                }
                for item in loaded
                if item is not None and item.get("__error__") is True
            ]
            self._inventory = [
                item
                for item in loaded
                if item is not None and item.get("__error__") is not True
            ]
            self._inventory_generation += 1
            self._inventory_created_at = monotonic()
            return self._inventory

    def _inventory_status(self) -> dict[str, Any]:
        return {
            "partial": bool(self._inventory_errors),
            "errors": [dict(error) for error in self._inventory_errors],
        }

    async def list_automations(self) -> dict[str, Any]:
        raw_states = sorted(
            await self._repository.list_automation_states(),
            key=lambda item: str(item.get("entity_id", "")),
        )
        truncated = len(raw_states) > MAX_AUTOMATIONS
        raw_states = raw_states[:MAX_AUTOMATIONS]
        automations: list[dict[str, Any]] = []
        for state in raw_states:
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str):
                continue
            automation_ref = self._automation_ref(entity_id)
            attributes = state.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            automations.append(
                {
                    "automation_ref": automation_ref,
                    "entity_id": entity_id,
                    "friendly_name": _sanitize_public_text(
                        attributes.get("friendly_name")
                    ),
                    "state": _safe_state(state.get("state")),
                    "last_triggered": _safe_timestamp(attributes.get("last_triggered")),
                }
            )
        payload = {
            "count": len(automations),
            "truncated": truncated,
            "max_automations": MAX_AUTOMATIONS,
            "automations": automations,
        }
        self._audit(payload, [], allow_raw_entity_ids=True)
        return payload

    async def get_automation_config(self, automation_ref: str) -> dict[str, Any]:
        entity_id = self._resolve_automation_ref(automation_ref)
        state = await self._repository.get_state(entity_id)
        raw_config = await self._repository.get_config(entity_id)
        internal_id = _resolve_internal_id(state, raw_config)
        anonymized = AutomationConfigSanitizer(PUBLIC_ANONYMIZATION_LIMITS).sanitize(raw_config)
        payload = {
            "automation_ref": automation_ref,
            "configuration": anonymized,
            "limits": _limits_payload(),
        }
        self._audit(payload, [internal_id], allow_raw_entity_ids=True)
        return payload

    async def list_automation_traces(
        self, automation_ref: str, max_traces: int = 10
    ) -> dict[str, Any]:
        if max_traces < 1 or max_traces > MAX_TRACES:
            raise AutomationDiagnosticsError(
                f"max_traces must be between 1 and {MAX_TRACES}"
            )
        entity_id, internal_id = await self._resolve_internal_id(automation_ref)
        raw_traces = await self._repository.list_traces(internal_id)
        if not isinstance(raw_traces, list):
            raise AutomationDiagnosticsError("Unexpected trace list response")
        if not raw_traces:
            raise AutomationDiagnosticsError(
                f"No traces exist for automation reference '{automation_ref}'"
            )
        ordered = sorted(raw_traces, key=_trace_sort_key, reverse=True)
        selected = ordered[:max_traces]
        traces: list[Any] = []
        forbidden = [internal_id]
        for raw_trace in selected:
            if not isinstance(raw_trace, dict):
                continue
            raw_run_id = raw_trace.get("run_id")
            if not isinstance(raw_run_id, str) or not raw_run_id:
                continue
            forbidden.append(raw_run_id)
            sanitized = AutomationConfigSanitizer(PUBLIC_ANONYMIZATION_LIMITS).sanitize(raw_trace)
            if isinstance(sanitized, dict):
                sanitized["run_ref"] = self._run_ref(automation_ref, raw_run_id)
                sanitized.pop("run_id", None)
            traces.append(sanitized)
        payload = {
            "automation_ref": automation_ref,
            "count": len(traces),
            "available_before_limit": len(raw_traces),
            "truncated": len(raw_traces) > len(selected),
            "max_traces": max_traces,
            "traces": traces,
        }
        self._audit(payload, forbidden, allow_raw_entity_ids=True)
        return payload

    async def get_automation_trace(
        self, automation_ref: str, run_ref: str
    ) -> dict[str, Any]:
        entity_id, internal_id = await self._resolve_internal_id(automation_ref)
        raw_run_id = self._resolve_run_ref(automation_ref, run_ref)
        raw_trace = await self._repository.get_trace(internal_id, raw_run_id)
        sanitized = AutomationConfigSanitizer(PUBLIC_ANONYMIZATION_LIMITS).sanitize(raw_trace)
        if not isinstance(sanitized, dict):
            raise AutomationDiagnosticsError("Unexpected trace response")
        sanitized["run_ref"] = run_ref
        sanitized.pop("run_id", None)
        payload = {
            "automation_ref": automation_ref,
            "trace": sanitized,
            "limits": _limits_payload(),
        }
        self._audit(payload, [internal_id, raw_run_id], allow_raw_entity_ids=True)
        return payload

    async def diagnose_automation_trace(
        self, automation_ref: str, run_ref: str
    ) -> dict[str, Any]:
        trace_payload = await self.get_automation_trace(automation_ref, run_ref)
        trace = trace_payload["trace"]
        trace_steps = trace.get("trace")
        if not isinstance(trace_steps, dict):
            trace_steps = {}
        condition_paths = [
            path for path in trace_steps if _path_contains(path, {"condition", "conditions"})
        ]
        branch_paths = [
            path
            for path in trace_steps
            if _path_contains(path, {"choose", "if", "then", "else", "default"})
        ]
        condition_results = _condition_results(trace_steps, condition_paths)
        branch_choices = _branch_choices(trace_steps, branch_paths)
        errors = _count_key(trace_steps, "error") + (1 if trace.get("error") else 0)
        markers = _marker_counts(trace_payload)
        duration = _trace_duration(trace.get("timestamp"))
        state = _safe_state(trace.get("state"))
        finding = _diagnostic_finding(state, errors, condition_results)
        return {
            "automation_ref": automation_ref,
            "run_ref": run_ref,
            "evidence": {
                "state": state,
                "duration_seconds": duration,
                "step_count": sum(
                    len(steps) for steps in trace_steps.values() if isinstance(steps, list)
                ),
                "condition_paths_evaluated": len(condition_paths),
                "condition_results": condition_results,
                "branch_paths_evaluated": len(branch_paths),
                "branch_choices": branch_choices,
                "error_count": errors,
                "redactions": dict(markers["redactions"]),
                "truncations": dict(markers["truncations"]),
            },
            "finding": finding,
            "suggestion": _diagnostic_suggestion(state, errors, condition_results),
            "human_summary": _human_summary(state, errors, condition_results),
            "confidence": 1.0 if errors or condition_results else 0.8,
        }

    async def _resolve_internal_id(self, automation_ref: str) -> tuple[str, str]:
        entity_id = self._resolve_automation_ref(automation_ref)
        state = await self._repository.get_state(entity_id)
        config = await self._repository.get_config(entity_id)
        return entity_id, _resolve_internal_id(state, config)

    def _automation_ref(self, entity_id: str) -> str:
        existing = self._automation_ref_by_entity.get(entity_id)
        if existing:
            return existing
        reference = f"automation_{len(self._automation_ref_by_entity) + 1:03d}"
        self._automation_ref_by_entity[entity_id] = reference
        self._automation_by_ref[reference] = entity_id
        return reference

    def _resolve_automation_ref(self, automation_ref: str) -> str:
        entity_id = self._automation_by_ref.get(automation_ref)
        if entity_id is None:
            raise AutomationDiagnosticsError(
                "Unknown automation reference; call ha_list_automations first"
            )
        return entity_id

    def _run_ref(self, automation_ref: str, run_id: str) -> str:
        key = (automation_ref, run_id)
        existing = self._run_ref_by_id.get(key)
        if existing:
            return existing
        prefix = (automation_ref,)
        count = sum(1 for item in self._run_ref_by_id if item[:1] == prefix) + 1
        reference = f"run_{count:03d}"
        self._run_ref_by_id[key] = reference
        self._run_by_ref[(automation_ref, reference)] = run_id
        return reference

    def _resolve_run_ref(self, automation_ref: str, run_ref: str) -> str:
        run_id = self._run_by_ref.get((automation_ref, run_ref))
        if run_id is None:
            raise AutomationDiagnosticsError(
                "Unknown run reference; call ha_list_automation_traces first"
            )
        return run_id

    @staticmethod
    def _audit(
        payload: dict[str, Any],
        forbidden: list[str],
        *,
        allow_raw_entity_ids: bool = False,
    ) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        audit_serialized_capture(
            {"mcp_response.json": serialized},
            forbidden_values=[value for value in forbidden if value],
            allow_raw_entity_ids=allow_raw_entity_ids,
            allow_trigger_ids=allow_raw_entity_ids,
        )


def _resolve_internal_id(state: Any, config_response: Any) -> str:
    candidates: list[Any] = []
    if isinstance(state, dict) and isinstance(state.get("attributes"), dict):
        candidates.append(state["attributes"].get("id"))
    if isinstance(config_response, dict):
        config = config_response.get("config")
        if isinstance(config, dict):
            candidates.append(config.get("id"))
        candidates.append(config_response.get("id"))
    for candidate in candidates:
        if isinstance(candidate, (str, int)) and str(candidate).strip():
            return str(candidate).strip()
    raise AutomationDiagnosticsError("Automation has no trace-compatible internal ID")


def _safe_state(value: Any) -> str:
    normalized = str(value).lower()
    allowed = {
        "on", "off", "running", "stopped", "debugged", "unknown", "unavailable"
    }
    return normalized if normalized in allowed else "unknown"


def _safe_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _trace_sort_key(trace: Any) -> datetime:
    if not isinstance(trace, dict) or not isinstance(trace.get("timestamp"), dict):
        return datetime.min
    value = trace["timestamp"].get("start")
    if not isinstance(value, str):
        return datetime.min
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return datetime.min


def _trace_duration(timestamp: Any) -> float | None:
    if not isinstance(timestamp, dict):
        return None
    start = _safe_timestamp(timestamp.get("start"))
    finish = _safe_timestamp(timestamp.get("finish"))
    if start is None or finish is None:
        return None
    started = datetime.fromisoformat(start.replace("Z", "+00:00"))
    finished = datetime.fromisoformat(finish.replace("Z", "+00:00"))
    return max((finished - started).total_seconds(), 0.0)


def _path_contains(path: Any, names: set[str]) -> bool:
    return any(segment in names for segment in str(path).split("/"))


def _condition_results(trace: dict[str, Any], paths: list[Any]) -> dict[str, int]:
    results = {"true": 0, "false": 0}
    for path in paths:
        steps = trace.get(path)
        if not isinstance(steps, list):
            continue
        for step in steps:
            result = step.get("result") if isinstance(step, dict) else None
            value = result.get("result") if isinstance(result, dict) else None
            if value is True:
                results["true"] += 1
            elif value is False:
                results["false"] += 1
    return results


def _branch_choices(trace: dict[str, Any], paths: list[Any]) -> int:
    count = 0
    for path in paths:
        steps = trace.get(path)
        if not isinstance(steps, list):
            continue
        for step in steps:
            result = step.get("result") if isinstance(step, dict) else None
            if isinstance(result, dict) and "choice" in result:
                count += 1
    return count


def _count_key(value: Any, wanted: str) -> int:
    if isinstance(value, dict):
        return sum((1 if key == wanted else 0) + _count_key(child, wanted) for key, child in value.items())
    if isinstance(value, list):
        return sum(_count_key(child, wanted) for child in value)
    return 0


def _marker_counts(value: Any) -> dict[str, Counter[str]]:
    redactions: Counter[str] = Counter()
    truncations: Counter[str] = Counter()

    def visit(child: Any) -> None:
        if isinstance(child, dict):
            for nested in child.values():
                visit(nested)
        elif isinstance(child, list):
            for nested in child:
                visit(nested)
        elif isinstance(child, str) and child.startswith("[REDACTED:"):
            redactions[child.removeprefix("[REDACTED:").removesuffix("]")] += 1
        elif isinstance(child, str) and child.startswith("[TRUNCATED:"):
            truncations[child.removeprefix("[TRUNCATED:").removesuffix("]")] += 1

    visit(value)
    return {"redactions": redactions, "truncations": truncations}


def _diagnostic_finding(state: str, errors: int, conditions: dict[str, int]) -> str:
    if errors:
        return "The execution contains one or more recorded errors."
    if conditions["false"]:
        return "One or more conditions evaluated to false."
    if state == "running":
        return "The execution was still running when the trace was read."
    return "No deterministic failure was found in the available trace."


def _diagnostic_suggestion(state: str, errors: int, conditions: dict[str, int]) -> str:
    if errors:
        return "Review the anonymized error-bearing trace steps."
    if conditions["false"]:
        return "Review the conditions that evaluated to false and their referenced state."
    if state == "running":
        return "Read the trace again after the execution finishes."
    return "No corrective action is suggested from this trace alone."


def _human_summary(state: str, errors: int, conditions: dict[str, int]) -> str:
    if errors:
        return "The automation ran, but Home Assistant recorded an error during execution."
    if conditions["false"]:
        return "The automation evaluated its conditions and at least one prevented progress."
    if state == "running":
        return "The automation had not finished when this diagnostic snapshot was taken."
    return "The available execution trace does not show a clear failure."


def _limits_payload() -> dict[str, int]:
    return {
        "max_depth": PUBLIC_ANONYMIZATION_LIMITS.max_depth,
        "max_string_length": PUBLIC_ANONYMIZATION_LIMITS.max_string_length,
        "max_collection_items": PUBLIC_ANONYMIZATION_LIMITS.max_collection_items,
        "max_total_bytes": PUBLIC_ANONYMIZATION_LIMITS.max_total_bytes,
    }


_AUTOMATION_SECRET_KEYS = frozenset(
    {
        "access_token", "api_key", "apikey", "authorization", "chat_id",
        "client_secret", "cookie", "credential", "credentials", "ha_token",
        "headers", "http_headers", "password", "refresh_token", "secret",
        "token", "webhook_id", "id", "automation_id", "context_id", "device_id",
        "item_id", "run_id", "user_id",
    }
)
_AUTOMATION_PRIVATE_CONTENT_KEYS = frozenset(
    {"body", "error", "message", "notification", "subject", "title"}
)
_URL_VALUE_RE = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)
_BEARER_VALUE_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_LONG_SECRET_VALUE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_~+/=-]{24,}(?![A-Za-z0-9])")
_EMBEDDED_SECRET_RE = re.compile(
    r"(?i)\b(?:token|password|passwd|api[_-]?key|client[_-]?secret|chat[_-]?id|webhook[_-]?id)\b\s*[:=]\s*[^\s,;}]+"
)
_CONFIG_ENTITY_ID_RE = re.compile(
    r"(?<![\w.])([a-z_][a-z0-9_]*\.[a-z0-9_]+)(?![\w.])",
    re.IGNORECASE,
)
_ENTITY_ID_FULL_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$", re.IGNORECASE)
_SERVICE_FULL_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$", re.IGNORECASE)
_STRUCTURED_ENTITY_KEYS = frozenset(
    {"entity_id", "event_entity_id", "source_entity_id", "target_entity_id"}
)
_TEMPLATE_KEY_PARTS = ("template", "value_template")


class AutomationConfigSanitizer:
    """Preserve automation semantics while redacting credentials and private endpoints."""

    def __init__(self, limits: AnonymizationLimits) -> None:
        self.limits = limits
        self.redactions: Counter[str] = Counter()
        self.truncated = False
        self._bytes = 0

    def sanitize(self, value: Any) -> Any:
        return self._visit(value, None, 0)

    def _marker(self, reason: str, *, truncated: bool = False) -> str:
        if truncated:
            self.truncated = True
            return f"[TRUNCATED:{reason}]"
        self.redactions[reason] += 1
        return f"[REDACTED:{reason}]"

    def _visit(self, value: Any, key: str | None, depth: int) -> Any:
        if depth >= self.limits.max_depth:
            return self._marker("depth", truncated=True)
        if self._bytes >= self.limits.max_total_bytes:
            return self._marker("total_size", truncated=True)
        normalized = key.casefold() if isinstance(key, str) else None
        is_top_level_internal_id = normalized == "id" and depth == 1
        if (normalized in _AUTOMATION_SECRET_KEYS and normalized != "id") or is_top_level_internal_id or (
            normalized is not None
            and any(part in normalized for part in ("token", "password", "secret", "webhook", "chat_id"))
        ):
            return self._marker("credential" if normalized not in {"chat_id", "webhook_id"} else normalized)
        if normalized in _AUTOMATION_PRIVATE_CONTENT_KEYS:
            return self._marker("private_content")
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            items = list(value.items())
            for raw_key, child in items[: self.limits.max_collection_items]:
                safe_key = str(raw_key)
                result[safe_key] = self._visit(child, safe_key, depth + 1)
            if len(items) > self.limits.max_collection_items:
                result["__truncated__"] = self._marker("items", truncated=True)
            return result
        if isinstance(value, (list, tuple)):
            result = [self._visit(child, key, depth + 1) for child in value[: self.limits.max_collection_items]]
            if len(value) > self.limits.max_collection_items:
                result.append(self._marker("items", truncated=True))
            return result
        if isinstance(value, str):
            replaced = _BEARER_VALUE_RE.sub(lambda _: self._marker("credential"), value)
            replaced = _URL_VALUE_RE.sub(lambda _: self._marker("url"), replaced)
            replaced = _EMBEDDED_SECRET_RE.sub(lambda _: self._marker("embedded_secret"), replaced)
            protected_entities: list[str] = []

            def protect_entity(match: re.Match[str]) -> str:
                protected_entities.append(match.group(1))
                return f"§{len(protected_entities) - 1}§"

            replaced = _CONFIG_ENTITY_ID_RE.sub(protect_entity, replaced)
            replaced = _LONG_SECRET_VALUE_RE.sub(lambda _: self._marker("possible_secret"), replaced)
            for index, entity in enumerate(protected_entities):
                replaced = replaced.replace(f"§{index}§", entity)
            if len(replaced) > self.limits.max_string_length:
                replaced = replaced[: self.limits.max_string_length] + self._marker("string", truncated=True)
            self._bytes += len(replaced.encode("utf-8"))
            return replaced
        if value is None or isinstance(value, (bool, int, float)):
            self._bytes += len(str(value))
            return value
        return self._marker("unsupported_type")


def _ensure_health_limit(max_results: int) -> None:
    if max_results < 1 or max_results > MAX_HEALTH_RESULTS:
        raise AutomationDiagnosticsError(
            f"max_results must be between 1 and {MAX_HEALTH_RESULTS}"
        )


def _catalog_error(error_code: str, message: str) -> dict[str, str]:
    return {"error_code": error_code, "message": message}


def _sanitize_public_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    sanitized = AutomationConfigSanitizer(PUBLIC_ANONYMIZATION_LIMITS).sanitize(value)
    return sanitized if isinstance(sanitized, str) else None


def _entity_health_status(entity_id: str, catalog: dict[str, Any]) -> str:
    state = catalog["states"].get(entity_id)
    registry = catalog["registry"].get(entity_id)
    if isinstance(registry, dict) and registry.get("disabled_by") is not None:
        return "disabled"
    if isinstance(state, dict):
        raw_state = str(state.get("state", "")).casefold()
        if raw_state == "unavailable":
            return "unavailable"
        if raw_state == "unknown":
            return "unknown"
        return "healthy"
    if isinstance(registry, dict):
        return "healthy"
    if catalog.get("states_available") and catalog.get("registry_available"):
        return "missing"
    return "indeterminate"


def _service_catalog(raw_services: Any) -> set[str]:
    if not isinstance(raw_services, list):
        raise ValueError("invalid service catalog")
    result: set[str] = set()
    for domain_entry in raw_services:
        if not isinstance(domain_entry, dict):
            continue
        domain = domain_entry.get("domain")
        services = domain_entry.get("services")
        if not isinstance(domain, str) or not isinstance(services, dict):
            continue
        for service in services:
            candidate = f"{domain}.{service}".casefold()
            if _SERVICE_FULL_RE.fullmatch(candidate):
                result.add(candidate)
    return result


def _config_digest(config: Any) -> str:
    return _digest_value(config)


def _digest_value(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def _entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0].casefold()


def _list_index(part: str) -> int | None:
    if part.startswith("[") and part.endswith("]") and part[1:-1].isdigit():
        return int(part[1:-1])
    return None


def _value_at_parts(value: Any, parts: tuple[str, ...]) -> Any:
    current = value
    for part in parts:
        index = _list_index(part)
        if index is not None:
            if not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _set_at_parts(value: Any, parts: tuple[str, ...], replacement: str) -> None:
    if not parts:
        raise AutomationDiagnosticsError("Automation occurrence path is invalid")
    parent = _value_at_parts(value, parts[:-1])
    final = parts[-1]
    index = _list_index(final)
    if index is not None and isinstance(parent, list) and index < len(parent):
        parent[index] = replacement
        return
    if isinstance(parent, dict) and final in parent:
        parent[final] = replacement
        return
    raise AutomationDiagnosticsError("Automation occurrence path is invalid")


def _collect_structured_references(config: Any) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, path_parts: list[str], value: str) -> None:
        path = _canonical_path(path_parts)
        key = (kind, path, value)
        if key not in seen:
            seen.add(key)
            references.append(
                {
                    "kind": kind,
                    "path": path,
                    "parts": list(path_parts),
                    "value": value,
                }
            )

    def visit(value: Any, parts: list[str], parent_key: str | None = None) -> None:
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key)
                child_parts = parts + [key]
                normalized = key.casefold()
                if normalized in _STRUCTURED_ENTITY_KEYS:
                    candidates = child if isinstance(child, list) else [child]
                    for index, candidate in enumerate(candidates):
                        if isinstance(candidate, str) and _ENTITY_ID_FULL_RE.fullmatch(candidate):
                            candidate_parts = (
                                child_parts + [f"[{index}]"]
                                if isinstance(child, list)
                                else child_parts
                            )
                            add("entity", candidate_parts, candidate)
                elif normalized in {"service", "action"} and isinstance(child, str):
                    if _SERVICE_FULL_RE.fullmatch(child):
                        add("service", child_parts, child)
                if isinstance(child, str) and (
                    "{{" in child
                    or "{%" in child
                    or any(part in normalized for part in _TEMPLATE_KEY_PARTS)
                ):
                    for match in _CONFIG_ENTITY_ID_RE.finditer(child):
                        add("template", child_parts, match.group(1))
                visit(child, child_parts, normalized)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, parts + [f"[{index}]"], parent_key)

    visit(config, [])
    return references


def _unwrap_config(response: Any) -> dict[str, Any]:
    if isinstance(response, dict) and isinstance(response.get("config"), dict):
        return response["config"]
    return response if isinstance(response, dict) else {}


def _internal_id_from_config(config: Any) -> str:
    if isinstance(config, dict) and isinstance(config.get("id"), (str, int)):
        return str(config["id"])
    return ""


def _safe_alias(item: dict[str, Any]) -> str | None:
    config = item.get("config")
    value = config.get("alias") if isinstance(config, dict) else None
    if not isinstance(value, str):
        value = item.get("attributes", {}).get("friendly_name")
    if not isinstance(value, str):
        return None
    return AutomationConfigSanitizer(PUBLIC_ANONYMIZATION_LIMITS).sanitize(value)


def _result_identity(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "automation_ref": item["automation_ref"],
        "entity_id": item["entity_id"],
        "alias": _safe_alias(item),
    }


def _canonical_path(parts: list[str]) -> str:
    aliases = {"trigger": "triggers", "condition": "conditions", "action": "actions"}
    result = ""
    for index, part in enumerate(parts):
        display = aliases.get(part, part) if index == 0 else part
        result += display if not result else f".{display}"
    return result.replace(".[", "[")


def _walk(value: Any, parts: list[str] | None = None):
    parts = parts or []
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, parts + [str(key)])
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, parts + [f"[{index}]"])
    else:
        yield parts, value


def _search_structure(config: dict[str, Any], needle: str) -> list[dict[str, Any]]:
    matches = []
    for parts, value in _walk(config):
        field = parts[-1] if parts else "value"
        if needle in str(value).casefold() or needle in field.casefold():
            matches.append({"path": _canonical_path(parts), "field": field, "value": value})
    return matches


def _find_exact_usage(config: dict[str, Any], query: str) -> list[dict[str, Any]]:
    usages = []
    for parts, value in _walk(config):
        if not isinstance(value, str) or value.casefold() != query.casefold():
            continue
        path = _canonical_path(parts)
        usages.append(
            {
                "section": path.split("[", 1)[0].split(".", 1)[0],
                "path": path,
                "field": parts[-1] if parts else "value",
                "value": value,
            }
        )
    return usages


def _summarize_steps(config: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []
    value = config.get(key, config.get(f"{key}s", []))
    steps = value if isinstance(value, list) else [value]
    summaries = []
    for index, step in enumerate(steps[:20]):
        if not isinstance(step, dict):
            summaries.append({"index": index, "type": type(step).__name__})
            continue
        summaries.append(
            {
                "index": index,
                "platform": step.get("platform") or step.get("trigger"),
                "action": step.get("action") or step.get("service"),
                "entity_id": step.get("entity_id") or (step.get("target", {}).get("entity_id") if isinstance(step.get("target"), dict) else None),
            }
        )
    return summaries
