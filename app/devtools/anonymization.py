from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from typing import Any


REDACTED_PREFIX = "[REDACTED:"
TRUNCATED_PREFIX = "[TRUNCATED:"

_ENTITY_ID_RE = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*\.[a-z0-9_]+)(?![\w.])", re.IGNORECASE)
_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_LONG_SECRET_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_~+/=-]{24,}(?![A-Za-z0-9])")
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_CANDIDATE_RE = re.compile(r"(?<![A-Fa-f0-9:])(?:[A-Fa-f0-9]{0,4}:){2,}[A-Fa-f0-9:]{0,4}(?![A-Fa-f0-9:])")
_COORDINATE_PAIR_RE = re.compile(
    r"(?<![A-Za-z0-9])[+-]?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)[,; ]+"
    r"[+-]?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d?\d(?:\.\d+)?)(?![A-Za-z0-9])"
)
_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_PROPOSAL_REF_RE = re.compile(r"proposal_[a-f0-9]{24}")
_DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}")

_CREDENTIAL_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "ha_token",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)
_HEADER_KEYS = frozenset({"headers", "http_headers", "request_headers"})
_PERSON_KEYS = frozenset(
    {
        "device_name",
        "display_name",
        "owner_name",
        "person",
        "person_name",
        "user_name",
    }
)
_VISIBLE_NAME_KEYS = frozenset({"alias", "friendly_name", "name"})
_LOCATION_KEYS = frozenset(
    {
        "address",
        "area_name",
        "city",
        "coordinates",
        "country",
        "gps",
        "latitude",
        "location",
        "longitude",
        "place",
        "postal_code",
        "street",
        "zone_name",
    }
)
_NOTIFICATION_KEYS = frozenset(
    {"body", "message", "notification", "notification_text", "subject", "title"}
)
_IDENTIFIER_KEYS = {
    "area_id": "area",
    "automation_id": "automation",
    "context_id": "context",
    "device_id": "device",
    "item_id": "automation",
    "parent_id": "context",
    "run_id": "run",
    "user_id": "user",
}
_TIMESTAMP_KEYS = frozenset(
    {"date", "finish", "last_changed", "last_reported", "last_triggered", "last_updated", "start", "timestamp", "time_fired"}
)
_SAFE_TEXT_KEYS = frozenset(
    {
        "code",
        "condition",
        "event",
        "for",
        "domain",
        "error_type",
        "last_step",
        "mode",
        "path",
        "platform",
        "script_execution",
        "service",
        "state",
        "trigger",
        "type",
        "unit_of_measurement",
    }
)
_PUBLIC_MACHINE_VALUES = frozenset(
    {
        "disabled",
        "healthy",
        "indeterminate",
        "missing",
        "possible_template_reference",
        "replace_structured_entity_references",
        "service_missing",
        "states_unavailable",
        "entity_registry_unavailable",
        "services_unavailable",
        "unavailable",
        "unknown",
    }
)


@dataclass(frozen=True)
class AnonymizationLimits:
    max_depth: int = 20
    max_string_length: int = 2_048
    max_collection_items: int = 500
    max_total_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        for field_name, value in asdict(self).items():
            if value <= 0:
                raise ValueError(f"{field_name} must be greater than zero")


class CaptureAuditError(RuntimeError):
    """Raised when a serialized capture still appears to contain private data."""

    def __init__(self, issue_codes: list[str]) -> None:
        self.issue_codes = tuple(sorted(set(issue_codes)))
        super().__init__(f"Capture audit failed ({', '.join(self.issue_codes)})")


class CaptureAnonymizer:
    """Create a bounded, non-reversible anonymized copy of HA payloads."""

    def __init__(self, limits: AnonymizationLimits | None = None) -> None:
        self.limits = limits or AnonymizationLimits()
        self._pseudonyms: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self._approximate_bytes = 0

    def anonymize(self, value: Any) -> Any:
        """Return a new anonymized value without modifying the input."""
        return self._anonymize(value, key=None, depth=0)

    def pseudonymize_identifier(self, category: str, value: Any) -> str:
        """Return a stable identifier suitable for sanitized summaries."""
        return self._pseudonym(category, str(value))

    def limits_metadata(self) -> dict[str, int]:
        return asdict(self.limits)

    def _anonymize(self, value: Any, *, key: str | None, depth: int) -> Any:
        if depth >= self.limits.max_depth:
            return self._marker("TRUNCATED", "depth")
        if self._approximate_bytes >= self.limits.max_total_bytes:
            return self._marker("TRUNCATED", "total_size")

        normalized_key = key.lower() if isinstance(key, str) else None
        if normalized_key in _CREDENTIAL_KEYS:
            return self._marker("REDACTED", "credential")
        if normalized_key in _HEADER_KEYS:
            return self._marker("REDACTED", "headers")
        is_collection = isinstance(value, (dict, list, tuple))
        if normalized_key in _PERSON_KEYS and not is_collection:
            return self._marker("REDACTED", "name")
        if normalized_key in _LOCATION_KEYS and not is_collection:
            return self._marker("REDACTED", "location")
        if normalized_key in _NOTIFICATION_KEYS and not is_collection:
            return self._marker("REDACTED", "notification")
        if normalized_key in _IDENTIFIER_KEYS and value is not None and not is_collection:
            return self._pseudonym(_IDENTIFIER_KEYS[normalized_key], str(value))
        if normalized_key == "id" and value is not None and not is_collection:
            return self._pseudonym("automation", str(value))

        if isinstance(value, dict):
            result: dict[str, Any] = {}
            items = list(value.items())
            for raw_key, child in items[: self.limits.max_collection_items]:
                safe_key = self._anonymize_mapping_key(str(raw_key))
                child_key = str(raw_key)
                if child_key.lower() == "id" and normalized_key == "context":
                    child_key = "context_id"
                elif _looks_like_entity_id(child_key):
                    child_key = "state"
                result[safe_key] = self._anonymize(
                    child, key=child_key, depth=depth + 1
                )
            if len(items) > self.limits.max_collection_items:
                result["__truncated__"] = self._marker("TRUNCATED", "items")
            return result

        if isinstance(value, (list, tuple)):
            result = [
                self._anonymize(child, key=key, depth=depth + 1)
                for child in value[: self.limits.max_collection_items]
            ]
            if len(value) > self.limits.max_collection_items:
                result.append(self._marker("TRUNCATED", "items"))
            return result

        if isinstance(value, str):
            return self._anonymize_string(value, normalized_key)

        if value is None or isinstance(value, (bool, int, float)):
            self._consume(value)
            return value

        return self._marker("REDACTED", "unsupported_type")

    def _anonymize_mapping_key(self, key: str) -> str:
        if _looks_like_entity_id(key):
            if _is_automation_entity_id(key):
                self._consume(key)
                return key
            return self._entity_pseudonym(key)
        if _has_sensitive_value_pattern(key):
            return self._pseudonym("key", key)
        self._consume(key)
        return key

    def _anonymize_string(self, value: str, key: str | None) -> str:
        if not value:
            return value
        if key in _TIMESTAMP_KEYS and _ISO_TIMESTAMP_RE.fullmatch(value):
            self._consume(value)
            return value
        if key == "entity_id" and _looks_like_entity_id(value):
            if _is_automation_entity_id(value):
                self._consume(value)
                return value
            return self._entity_pseudonym(value)

        replaced = _URL_RE.sub(lambda match: self._pseudonym("url", match.group(0)), value)
        replaced = _EMAIL_RE.sub(lambda match: self._pseudonym("email", match.group(0)), replaced)
        replaced = _ENTITY_ID_RE.sub(
            lambda match: (
                match.group(1)
                if _is_automation_entity_id(match.group(1))
                else self._entity_pseudonym(match.group(1))
            ),
            replaced,
        )
        replaced = _BEARER_RE.sub(self._redact_match("credential"), replaced)
        replaced = _replace_ip_addresses(replaced, self)
        replaced = _COORDINATE_PAIR_RE.sub(self._redact_match("location"), replaced)

        if key not in _TIMESTAMP_KEYS:
            replaced = _LONG_SECRET_RE.sub(self._redact_match("possible_secret"), replaced)

        if key == "state" and replaced == value and not _is_safe_state(value):
            return self._marker("REDACTED", "state")
        if key not in _SAFE_TEXT_KEYS and key not in _VISIBLE_NAME_KEYS and replaced == value:
            return self._marker("REDACTED", "free_text")

        if len(replaced) > self.limits.max_string_length:
            replaced = (
                replaced[: self.limits.max_string_length]
                + self._marker("TRUNCATED", "string")
            )
        self._consume(replaced)
        return replaced

    def _redact_match(self, kind: str):
        def replace(_: re.Match[str]) -> str:
            return self._marker("REDACTED", kind)

        return replace

    def _entity_pseudonym(self, value: str) -> str:
        domain, _, _ = value.partition(".")
        normalized_domain = domain.lower() if re.fullmatch(r"[a-z_][a-z0-9_]*", domain, re.IGNORECASE) else "unknown"
        suffix = self._pseudonym("entity", value).removeprefix("entity_")
        pseudonym = f"{normalized_domain}.entity_{suffix}"
        self._consume(pseudonym)
        return pseudonym

    def _pseudonym(self, category: str, value: str) -> str:
        lookup = (category, value)
        if lookup not in self._pseudonyms:
            counter = self._counters.get(category, 0) + 1
            self._counters[category] = counter
            self._pseudonyms[lookup] = f"{category}_{counter:03d}"
        pseudonym = self._pseudonyms[lookup]
        self._consume(pseudonym)
        return pseudonym

    def _marker(self, marker_type: str, reason: str) -> str:
        marker = f"[{marker_type}:{reason}]"
        self._consume(marker)
        return marker

    def _consume(self, value: Any) -> None:
        try:
            self._approximate_bytes += len(
                json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            )
        except (TypeError, ValueError):
            self._approximate_bytes += len(str(value).encode("utf-8"))


def audit_serialized_capture(
    serialized_documents: dict[str, str],
    *,
    forbidden_values: list[str] | tuple[str, ...] = (),
    allow_raw_entity_ids: bool = False,
    allow_trigger_ids: bool = False,
) -> None:
    """Reject serialized capture documents that still look sensitive."""
    issue_codes: list[str] = []
    for serialized in serialized_documents.values():
        try:
            parsed_document = json.loads(serialized)
        except (TypeError, json.JSONDecodeError):
            issue_codes.append("invalid_json")
            continue
        string_values = list(_iter_string_values(parsed_document))
        lowered = serialized.lower()
        if any(scheme in lowered for scheme in ("http://", "https://", "ws://", "wss://")):
            issue_codes.append("url")
        if "bearer " in lowered:
            issue_codes.append("bearer_token")
        if _EMAIL_RE.search(serialized):
            issue_codes.append("email")
        if _contains_ip_address(serialized):
            issue_codes.append("ip_address")
        if any(_COORDINATE_PAIR_RE.search(value) for value in string_values) or re.search(
            r'"(?:latitude|longitude|coordinates|gps)"\s*:\s*-?\d',
            serialized,
            re.IGNORECASE,
        ):
            issue_codes.append("coordinates")
        if any(
            _LONG_SECRET_RE.search(
                _DIGEST_RE.sub(
                    "",
                    _PROPOSAL_REF_RE.sub(
                        "",
                        _ENTITY_ID_RE.sub("", value) if allow_raw_entity_ids else value,
                    ),
                )
            )
            and not _is_automation_entity_id(value)
            and value not in _PUBLIC_MACHINE_VALUES
            for value in string_values
        ):
            issue_codes.append("possible_secret")
        issue_codes.extend(
            _audit_identifier_shapes(
                parsed_document,
                allow_raw_entity_ids=allow_raw_entity_ids,
                allow_trigger_ids=allow_trigger_ids,
            )
        )
        for forbidden in forbidden_values:
            if not forbidden:
                continue
            encoded = json.dumps(forbidden, ensure_ascii=False)
            if forbidden in serialized or encoded in serialized:
                issue_codes.append("known_identifier")
                break
    if issue_codes:
        raise CaptureAuditError(issue_codes)


def _looks_like_entity_id(value: str) -> bool:
    return _ENTITY_ID_RE.fullmatch(value) is not None


def _is_automation_entity_id(value: str) -> bool:
    return value.lower().startswith("automation.") and _looks_like_entity_id(value)


def _has_sensitive_value_pattern(value: str) -> bool:
    return bool(
        _URL_RE.search(value)
        or _EMAIL_RE.search(value)
        or _BEARER_RE.search(value)
        or _contains_ip_address(value)
    )


def _is_safe_state(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {
        "on",
        "off",
        "unknown",
        "unavailable",
        "running",
        "stopped",
        "debugged",
        "idle",
        "home",
        "not_home",
    }:
        return True
    try:
        float(normalized)
    except ValueError:
        return False
    return True


def _replace_ip_addresses(value: str, anonymizer: CaptureAnonymizer) -> str:
    def replace(candidate: re.Match[str]) -> str:
        try:
            ipaddress.ip_address(candidate.group(0))
        except ValueError:
            return candidate.group(0)
        return anonymizer._pseudonym("ip", candidate.group(0))

    value = _IPV4_RE.sub(replace, value)
    return _IPV6_CANDIDATE_RE.sub(replace, value)


def _contains_ip_address(value: str) -> bool:
    for pattern in (_IPV4_RE, _IPV6_CANDIDATE_RE):
        for match in pattern.finditer(value):
            try:
                ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            return True
    return False


def _iter_string_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_string_values(child)


def _audit_identifier_shapes(
    value: Any,
    *,
    parent_key: str | None = None,
    allow_raw_entity_ids: bool = False,
    allow_trigger_ids: bool = False,
) -> list[str]:
    issues: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            category = _identifier_category_for_audit(key, parent_key)
            if category is not None and not (
                allow_raw_entity_ids and category == "entity"
            ) and not (
                allow_trigger_ids and key == "id"
            ):
                issues.extend(_audit_identifier_value(child, category))
            issues.extend(
                _audit_identifier_shapes(
                    child,
                    parent_key=key,
                    allow_raw_entity_ids=allow_raw_entity_ids,
                    allow_trigger_ids=allow_trigger_ids,
                )
            )
    elif isinstance(value, list):
        for child in value:
            issues.extend(
                _audit_identifier_shapes(
                    child,
                    parent_key=parent_key,
                    allow_raw_entity_ids=allow_raw_entity_ids,
                    allow_trigger_ids=allow_trigger_ids,
                )
            )
    return issues


def _identifier_category_for_audit(key: str, parent_key: str | None) -> str | None:
    if key == "id":
        return "context" if parent_key == "context" else "automation"
    if key == "entity_id":
        return "entity"
    return _IDENTIFIER_KEYS.get(key)


def _audit_identifier_value(value: Any, category: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        issues: list[str] = []
        for child in value:
            issues.extend(_audit_identifier_value(child, category))
        return issues
    if not isinstance(value, str):
        return [f"raw_{category}_id"]
    if re.fullmatch(r"\[(?:REDACTED|TRUNCATED):[a-z_]+\]", value):
        return []
    if category == "entity":
        if _is_automation_entity_id(value) or re.fullmatch(
            r"[a-z_][a-z0-9_]*\.entity_\d{3}", value, re.IGNORECASE
        ):
            return []
    elif re.fullmatch(rf"{re.escape(category)}_\d{{3}}", value):
        return []
    return [f"raw_{category}_id"]
