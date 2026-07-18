from __future__ import annotations

from app.config import Settings
from app.main import _build_oauth_authorization_metadata


def _make_settings(**overrides: object) -> Settings:
    values = dict(
        ha_url="http://ha.local:8123",
        ha_token="test-token",
        mcp_host="127.0.0.1",
        mcp_port=8000,
        log_level="INFO",
        entities_file="config/entities.yaml",
        request_timeout_seconds=5.0,
        oauth_metadata_enabled=True,
        oauth_issuer_url=None,
        oauth_resource_server_url=None,
        oauth_required_scopes="",
    )
    values.update(overrides)
    return Settings.model_construct(**values)


def test_oauth_authorization_metadata_uses_local_defaults() -> None:
    settings = _make_settings(oauth_required_scopes="mcp:read,mcp:tools")

    metadata = _build_oauth_authorization_metadata(settings)

    assert str(metadata.issuer) == "http://127.0.0.1:8000/"
    assert str(metadata.authorization_endpoint) == "http://127.0.0.1:8000/authorize"
    assert str(metadata.token_endpoint) == "http://127.0.0.1:8000/token"
    assert metadata.scopes_supported == ["mcp:read", "mcp:tools"]


def test_settings_compute_resource_server_url_from_mcp_path() -> None:
    settings = _make_settings()

    assert settings.resolved_oauth_issuer_url == "http://127.0.0.1:8000"
    assert settings.resolved_oauth_resource_server_url == "http://127.0.0.1:8000/mcp"
