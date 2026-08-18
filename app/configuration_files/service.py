from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Mapping

from ..inventory import InventoryStore
from .provider import ConfigAccessError, HomeAssistantConfigProvider, MAX_YAML_BYTES


MAX_PAGE_SIZE = 100
ENTITY_PATTERN = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*\.[a-z0-9_]+)(?![\w.])", re.I)
SECRET_REFERENCE = re.compile(r"!secret(?:\s+[^\s,}\]]+)?", re.I)
SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|token|api[_-]?key|secret|credential|private[_-]?key|access[_-]?key|client[_-]?secret)"
)
SENSITIVE_LINE = re.compile(r"^(\s*[^#\n:]+:\s*)(.*)$")
BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
URL_CREDENTIAL = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
URL_SECRET_QUERY = re.compile(r"(?i)([?&](?:token|api[_-]?key|access[_-]?token|key)=)[^&#\s]+")
INLINE_SECRET = re.compile(
    r"(?i)((?:token|password|api[_-]?key|client[_-]?secret)\s*[:=]\s*)[^\s,}\]]+"
)
LONG_SECRET = re.compile(r"(?<![\w.])(?:eyJ[a-zA-Z0-9_-]{20,}|[a-zA-Z0-9_+=/-]{40,})(?![\w.])")
YAML_FILENAME = re.compile(r"(?im)^\s*filename\s*:\s*['\"]?([^'\"#\s]+\.ya?ml)")
VIEWS_KEY = re.compile(r"(?m)^views\s*:")


class HomeAssistantConfigService:
    def __init__(self, provider: HomeAssistantConfigProvider, inventory: InventoryStore) -> None:
        self._provider = provider
        self._inventory = inventory

    def list_yaml(self, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        items = [
            {
                "path": item.path,
                "size": item.size,
                "modified_at": item.modified_at,
                "readable": PurePosixPath(item.path).name.casefold() != "secrets.yaml"
                and item.size <= MAX_YAML_BYTES,
            }
            for item in self._provider.yaml_files()
        ]
        return self._page("yaml", items, cursor, limit)

    def read_yaml(self, path: str) -> dict[str, Any]:
        content = _sanitize_text(self._provider.read_yaml(path))
        return self._response(path, content, "yaml")

    def search_yaml(
        self, query: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        needle = query.strip().casefold()
        if not needle or len(needle) > 200:
            raise ConfigAccessError("Search query must contain between 1 and 200 characters")
        matches: list[dict[str, Any]] = []
        for item in self._provider.yaml_files():
            if item.size > MAX_YAML_BYTES or PurePosixPath(item.path).name.casefold() == "secrets.yaml":
                continue
            try:
                lines = _sanitize_text(self._provider.read_yaml(item.path)).splitlines()
            except ConfigAccessError:
                continue
            for number, line in enumerate(lines, 1):
                if needle in line.casefold():
                    matches.append(
                        {
                            "path": item.path,
                            "line": number,
                            "text": line[:500],
                            "entities": self._entity_relations(line),
                        }
                    )
        return self._page(f"search:{hashlib.sha256(needle.encode()).hexdigest()}", matches, cursor, limit)

    def list_dashboards(self, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        dashboards: dict[str, dict[str, Any]] = {}
        for item in self._provider.storage_dashboards():
            ref = f"storage:{item.path}"
            dashboards[ref] = {
                "dashboard_ref": ref, "source": "storage", "path": item.path,
                "size": item.size, "modified_at": item.modified_at,
            }
        referenced: set[str] = set()
        registry = self._provider.dashboard_registry()
        if registry is not None and registry.size <= MAX_YAML_BYTES:
            try:
                registry_data = json.loads(self._provider.read_dashboard_registry())
                entries = registry_data.get("data", {}).get("items", [])
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, Mapping) and isinstance(entry.get("filename"), str):
                            referenced.add(entry["filename"])
            except (ConfigAccessError, json.JSONDecodeError, AttributeError):
                pass
        yaml_files = self._provider.yaml_files()
        for item in yaml_files:
            if item.size > MAX_YAML_BYTES or PurePosixPath(item.path).name.casefold() == "secrets.yaml":
                continue
            try:
                content = self._provider.read_yaml(item.path)
            except ConfigAccessError:
                continue
            referenced.update(match.group(1) for match in YAML_FILENAME.finditer(content))
            if PurePosixPath(item.path).name.startswith("ui-lovelace") or VIEWS_KEY.search(content):
                referenced.add(item.path)
        by_path = {item.path: item for item in yaml_files}
        for path in sorted(referenced):
            item = by_path.get(path)
            if item is None:
                continue
            ref = f"yaml:{path}"
            dashboards[ref] = {
                "dashboard_ref": ref, "source": "yaml", "path": item.path,
                "size": item.size, "modified_at": item.modified_at,
            }
        return self._page("dashboards", list(dashboards.values()), cursor, limit)

    def read_dashboard(self, dashboard_ref: str) -> dict[str, Any]:
        source, separator, path = dashboard_ref.partition(":")
        if not separator or source not in {"yaml", "storage"}:
            raise ConfigAccessError("Invalid dashboard reference")
        raw = self._provider.read_yaml(path) if source == "yaml" else self._provider.read_dashboard_storage(path)
        content = _sanitize_json(raw) if source == "storage" else _sanitize_text(raw)
        return self._response(path, content, source) | {"dashboard_ref": dashboard_ref}

    def list_text(self, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        items = [
            {"path": item.path, "size": item.size, "modified_at": item.modified_at}
            for item in self._provider.text_files()
        ]
        return self._page("text-files", items, cursor, limit)

    def read_text(
        self, path: str, cursor: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        metadata, raw = self._provider.read_text_file(path)
        content = _sanitize_json(raw) if path.casefold().startswith(".storage/") else _sanitize_text(raw)
        lines = []
        for number, line in enumerate(content.splitlines(), 1):
            segments = [line[index : index + 4000] for index in range(0, max(1, len(line)), 4000)]
            for segment_number, segment in enumerate(segments, 1):
                lines.append(
                    {
                        "line": number,
                        "segment": segment_number,
                        "text": segment,
                        "entities": self._entity_relations(segment),
                    }
                )
        scope = f"text:{path}:{metadata.size}:{metadata.modified_at}"
        return self._page(scope, lines, cursor, limit) | {
            "path": path,
            "size": metadata.size,
            "modified_at": metadata.modified_at,
            "inventory_generation": self._inventory.snapshot.generation,
        }

    def search_text(
        self, query: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        needle = query.strip().casefold()
        if not needle or len(needle) > 200:
            raise ConfigAccessError("Search query must contain between 1 and 200 characters")
        matches: list[dict[str, Any]] = []
        for metadata in self._provider.text_files():
            try:
                _, raw = self._provider.read_text_file(metadata.path)
            except ConfigAccessError:
                continue
            content = (
                _sanitize_json(raw)
                if metadata.path.casefold().startswith(".storage/")
                else _sanitize_text(raw)
            )
            for number, line in enumerate(content.splitlines(), 1):
                if needle in line.casefold():
                    matches.append(
                        {
                            "path": metadata.path,
                            "line": number,
                            "text": line[:500],
                            "entities": self._entity_relations(line),
                        }
                    )
        scope = f"text-search:{hashlib.sha256(needle.encode()).hexdigest()}"
        return self._page(scope, matches, cursor, limit) | {
            "inventory_generation": self._inventory.snapshot.generation,
        }

    def _response(self, path: str, content: str, source: str) -> dict[str, Any]:
        return {
            "path": path,
            "source": source,
            "content": content,
            "entities": self._entity_relations(content),
            "inventory_generation": self._inventory.snapshot.generation,
            "write_capability": False,
        }

    def _entity_relations(self, content: str) -> list[dict[str, Any]]:
        snapshot = self._inventory.snapshot
        relations = []
        for entity_id in sorted(set(ENTITY_PATTERN.findall(content))):
            record = snapshot.entities.get(entity_id)
            relations.append(
                {
                    "entity_id": entity_id,
                    "known": record is not None,
                    "device_id": record.get("device_id") if record else None,
                    "area_id": record.get("area_id") if record else None,
                    "integration_id": record.get("config_entry_id") if record else None,
                }
            )
        return relations

    @staticmethod
    def _page(scope: str, items: list[dict[str, Any]], cursor: str | None, limit: int) -> dict[str, Any]:
        if limit < 1 or limit > MAX_PAGE_SIZE:
            raise ConfigAccessError("limit must be between 1 and 100")
        offset = _decode_cursor(cursor, scope)
        page = items[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "items": page,
            "pagination": {
                "limit": limit,
                "returned": len(page),
                "available": len(items),
                "truncated": next_offset < len(items),
                "next_cursor": _encode_cursor(scope, next_offset) if next_offset < len(items) else None,
            },
            "write_capability": False,
        }


def _sanitize_text(content: str) -> str:
    sanitized = SECRET_REFERENCE.sub("!secret [referenced]", content)
    lines = []
    for line in sanitized.splitlines():
        match = SENSITIVE_LINE.match(line)
        if match and SENSITIVE_KEY.search(match.group(1)):
            replacement = "!secret [referenced]" if "!secret [referenced]" in match.group(2) else "[REDACTED]"
            line = f"{match.group(1)}{replacement}"
        line = BEARER.sub("Bearer [REDACTED]", line)
        line = URL_CREDENTIAL.sub(r"\1[REDACTED]@", line)
        line = URL_SECRET_QUERY.sub(r"\1[REDACTED]", line)
        if "!secret [referenced]" not in line:
            line = INLINE_SECRET.sub(r"\1[REDACTED]", line)
        line = LONG_SECRET.sub("[REDACTED]", line)
        lines.append(line)
    return "\n".join(lines)


def _sanitize_json(content: str) -> str:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return _sanitize_text(content)

    def clean(value: Any, key: str = "") -> Any:
        if SENSITIVE_KEY.search(key):
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {str(item_key): clean(item, str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            return _sanitize_text(value)
        return value

    return json.dumps(clean(parsed), indent=2, sort_keys=True, ensure_ascii=False)


def _encode_cursor(scope: str, offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"s": scope, "o": offset}, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None, scope: str) -> int:
    if cursor is None:
        return 0
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if payload.get("s") != scope or not isinstance(payload.get("o"), int) or payload["o"] < 0:
            raise ValueError
        return payload["o"]
    except Exception:
        raise ConfigAccessError("Invalid cursor") from None
