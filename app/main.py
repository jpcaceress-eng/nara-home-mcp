from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthMetadata
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .clients import HomeAssistantClient, HomeAssistantError, HomeAssistantWebSocketClient
from .configuration import EntitiesConfig, Settings, load_entities_config
from .policy import is_sensitive_domain
from .tools import register_tools
from .inventory import InventoryCollector, InventoryNormalizer, InventoryScheduler, InventoryStore
from .configuration_files import HomeAssistantConfigProvider


THIRD_PARTY_SENSITIVE_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "websockets",
    "websockets.client",
    "websockets.server",
    "uvicorn.access",
    "uvicorn.error",
)


def _configure_third_party_logging() -> None:
    """Keep third-party request metadata out of INFO logs."""
    for logger_name in THIRD_PARTY_SENSITIVE_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


@dataclass
class AppContext:
    settings: Settings
    entities: EntitiesConfig
    ha_client: HomeAssistantClient
    ha_websocket_client: HomeAssistantWebSocketClient


def _resolve_entities_path(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def load_runtime_entities(settings: Settings) -> EntitiesConfig:
    """Load exactly the configured entity file and fail closed on any error."""
    return load_entities_config(_resolve_entities_path(settings.entities_file))


def _detect_git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def _build_oauth_authorization_metadata(settings: Settings) -> OAuthMetadata:
    issuer = settings.resolved_oauth_issuer_url.rstrip("/")
    scopes_supported = settings.oauth_required_scopes_list or None
    return OAuthMetadata(
        issuer=issuer,
        authorization_endpoint=f"{issuer}/authorize",
        token_endpoint=f"{issuer}/token",
        scopes_supported=scopes_supported,
        response_types_supported=["code"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic"],
        code_challenge_methods_supported=["S256"],
    )


def _register_health_route(mcp: FastMCP) -> None:
    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "write_capability": False},
            headers={"Cache-Control": "no-store"},
        )


async def _expand_raw_entity_allowlist(
    ha_client: HomeAssistantClient,
    entities: EntitiesConfig,
    *,
    enabled: bool = False,
) -> None:
    if not enabled:
        return

    current_allowed = set(entities.allowed_raw_entities)
    try:
        for state in await ha_client.list_states():
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or is_sensitive_domain(entity_id):
                continue
            current_allowed.add(entity_id.strip())
    except HomeAssistantError as exc:
        logging.getLogger(__name__).warning("Could not expand HA allowlist at startup: %s", exc)
        return
    entities.allowed_raw_entities = sorted(current_allowed)


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _configure_third_party_logging()
    entities = load_runtime_entities(settings)

    ha_client = HomeAssistantClient(
        base_url=settings.ha_url,
        token=settings.ha_token,
        timeout_seconds=settings.request_timeout_seconds,
    )
    ha_websocket_client = HomeAssistantWebSocketClient(
        base_url=settings.ha_url,
        token=settings.ha_token,
        timeout_seconds=settings.request_timeout_seconds,
        websocket_url=settings.ha_websocket_url,
    )
    inventory_store = InventoryStore(
        InventoryCollector(
            client_factory=lambda: HomeAssistantWebSocketClient(
                base_url=settings.ha_url,
                token=settings.ha_token,
                timeout_seconds=settings.request_timeout_seconds,
                websocket_url=settings.ha_websocket_url,
            )
        ),
        InventoryNormalizer(),
    )
    inventory_scheduler = InventoryScheduler(
        inventory_store,
        interval_seconds=settings.inventory_refresh_interval_seconds,
        timeout_seconds=settings.inventory_refresh_timeout_seconds,
        retry_base_seconds=settings.inventory_retry_base_seconds,
        retry_max_seconds=settings.inventory_retry_max_seconds,
        jitter_ratio=settings.inventory_retry_jitter_ratio,
    )

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[AppContext]:
        # Uvicorn installs its logging configuration immediately before lifespan.
        _configure_third_party_logging()
        try:
            await _expand_raw_entity_allowlist(
                ha_client,
                entities,
                enabled=settings.allow_dynamic_entities,
            )
            yield AppContext(
                settings=settings,
                entities=entities,
                ha_client=ha_client,
                ha_websocket_client=ha_websocket_client,
            )
        finally:
            await ha_websocket_client.aclose()
            await ha_client.aclose()

    transport_security = None
    if settings.allowed_hosts_list or settings.allowed_origins_list:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts_list,
            allowed_origins=settings.allowed_origins_list,
        )

    mcp = FastMCP(
        name=settings.mcp_server_name,
        instructions=(
            "Read-only Home Assistant MCP server with dynamic operational data, "
            "Recorder history/statistics, Logbook, registries, diagnostics, and configuration files."
        ),
        stateless_http=True,
        json_response=True,
        lifespan=lifespan,
        transport_security=transport_security,
    )
    mcp.settings.host = settings.mcp_host
    mcp.settings.port = settings.mcp_port
    mcp.settings.streamable_http_path = "/mcp"

    _register_health_route(mcp)
    if settings.oauth_metadata_enabled:
        mcp.settings.auth = AuthSettings(
            issuer_url=settings.resolved_oauth_issuer_url,
            resource_server_url=settings.resolved_oauth_resource_server_url,
            required_scopes=settings.oauth_required_scopes_list or None,
        )

        @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
        async def oauth_authorization_server_metadata(_: Request) -> Response:
            metadata = _build_oauth_authorization_metadata(settings)
            return JSONResponse(metadata.model_dump(mode="json", exclude_none=True))

    started_at = datetime.now(timezone.utc)
    git_commit = _detect_git_commit()
    register_tools(
        mcp,
        ha_client,
        entities,
        started_at=started_at,
        git_commit=git_commit,
        automation_websocket=ha_websocket_client,
        inventory_store=inventory_store,
        inventory_scheduler=inventory_scheduler,
        ha_config_provider=HomeAssistantConfigProvider(
            settings.config_root,
            require_cifs=settings.config_require_read_only_mount,
            enabled=settings.config_read_enabled,
        ),
    )
    registered_tool_names = [tool.name for tool in mcp._tool_manager.list_tools()]
    logging.getLogger(__name__).info(
        "Registered MCP tools (%d): %s",
        len(registered_tool_names),
        ", ".join(sorted(registered_tool_names)),
    )
    inventory_scheduler.start_background()
    inventory_scheduler.wait_for_first_attempt(
        settings.inventory_refresh_timeout_seconds + 1.0
    )
    try:
        mcp.run(transport="streamable-http")
    finally:
        inventory_scheduler.stop_background()


if __name__ == "__main__":
    main()
