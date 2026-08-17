# Nara Home MCP

Nara Home is a read-only Model Context Protocol server for Home Assistant. It exposes current states, complete attributes, Recorder history and statistics, Logbook events, registries, automation diagnostics, dashboards, and optional configuration-file reads through 43 general MCP tools.

The current public snapshot is V4/4B.6. It cannot call Home Assistant services and declares `write_capability: false`. Older commits remain in Git for project history; their control tools are not part of the current version.

## Current capabilities

- Dynamic discovery from `/api/states` on every state-list request.
- Complete state and attribute reads for every valid entity domain.
- Multi-entity Recorder history with ISO 8601 start/end times, pagination, and resumable 24-hour fragments.
- Short- and long-term Recorder statistics with metadata, units, mean, minimum, maximum, sum, state, change, and last reset when Home Assistant provides them.
- Paginated Logbook reads across arbitrary retained periods.
- Entity, device, area, integration/config-entry, and service inventory with automatic background refresh.
- Read-only automation, script, scene, trace, dashboard, YAML, and internal-text diagnostics.
- Credential-focused redaction without hiding normal entity IDs, names, states, or attributes.

Aliases in `entities.yaml` are optional conveniences. They are not an authorization boundary for general state, history, statistics, or Logbook reads.

## Security model

The REST client rejects every method except `GET`. The WebSocket client uses a closed allowlist containing only read commands. There are no public tools for turning devices on or off, invoking scenes, changing brightness, calling services, editing configuration, or writing statistics.

Operational payloads redact credential-bearing keys, authorization values, JWTs, and credentials embedded in URLs. Home Assistant URLs and tokens must be supplied at runtime and must never be committed.

Optional configuration-file access is disabled by default. When enabled, the root must be supplied explicitly and is still subject to path, symlink, file-type, size, sensitive-file, and read-only-mount checks.

See [SECURITY.md](SECURITY.md) for reporting and deployment guidance.

## Requirements

- Python 3.11 or newer.
- Home Assistant with REST and WebSocket APIs available.
- A dedicated Home Assistant access token with the minimum permissions required for reads.
- HTTPS and authentication in front of MCP when it is reachable outside a trusted network.

## Local installation

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp examples/env.example .env
cp config/entities.example.yaml config/entities.yaml
```

Set `HA_URL` and `HA_TOKEN` in `.env`, then run:

```bash
python -m app.main
```

The default endpoint is `http://127.0.0.1:8000/mcp`.

## Configuration

Core settings:

- `HA_URL` and `HA_TOKEN`: Home Assistant connection.
- `MCP_HOST` and `MCP_PORT`: local MCP listener.
- `ENTITIES_FILE`: optional alias/favorites file.
- `ALLOWED_HOSTS` and `ALLOWED_ORIGINS`: transport restrictions.
- `OAUTH_METADATA_ENABLED`, `OAUTH_ISSUER_URL`, `OAUTH_RESOURCE_SERVER_URL`, and `OAUTH_REQUIRED_SCOPES`: optional external MCP authentication metadata.

Optional configuration reads:

- `HA_CONFIG_READ_ENABLED=false`: explicit feature switch.
- `HA_CONFIG_ROOT`: no default; required when the feature is enabled.
- `HA_CONFIG_REQUIRE_READ_ONLY_MOUNT=true`: fail closed unless the configured root is a read-only mount accepted by the current provider.

The systemd file under `systemd/` is a template. Render its user, group, and installation-directory placeholders for the target host. Home Assistant App and Docker packaging are intentionally outside this snapshot.

## Development

Runtime and development dependencies are separated in `pyproject.toml`. Install the development extra and run focused tests with `pytest`. The repository intentionally keeps the existing Git history, but only the current branch tip describes the public read-only product.

## Data availability

Nara can return only data that Home Assistant exposes and retains. Disabled entities may exist in the entity registry without a live state. Recorder exclusions or retention may yield `not_recorded`; an entity absent from live states yields `not_available`. Statistics and Logbook events are unavailable when Home Assistant never created or has already purged them.

## License

MIT. See [LICENSE](LICENSE).
