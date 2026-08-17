# Nara Home App

Nara Home exposes read-only Home Assistant operational data through MCP. This
App skeleton supports `amd64` and `aarch64` and intentionally has no ingress,
host-network access, host port, Docker API, devices, privileged capabilities,
Supervisor API access, OAuth, tunnel, or remote publication.

## Configuration

- **Log level** controls App log verbosity.
- **Read internal configuration files** is disabled by default. Enabling it
  permits Nara's existing guarded, read-only file tools to inspect eligible text
  files below the fixed `/homeassistant_config` mount. The user cannot select a
  different root.

The Home Assistant configuration directory is always mounted read-only. The App
uses `http://supervisor/core/api` for REST,
`ws://supervisor/core/websocket` for WebSocket, and obtains its bearer token only
from the Supervisor-provided `SUPERVISOR_TOKEN` environment variable. The token
is not an App option and is not written to disk or logs.

## Network and health

The MCP server listens on port 8000 only inside the App container. The host port
mapping is disabled. Supervisor checks `/health` through the internal App
network. A future private connection mechanism must be designed separately.

## Read-only contract

The App retains exactly 43 MCP tools and reports `write_capability: false`. Its
REST client rejects every method except GET and its WebSocket client permits only
the established read-command allowlist.
