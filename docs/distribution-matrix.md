# Distribution matrix

Status date: 2026-08-17. This document describes distribution options; it does
not add a Home Assistant App, container image, tunnel, OAuth service, or public
endpoint.

## Supported Home Assistant installations

Home Assistant currently offers two supported installation methods:
[Home Assistant OS and Home Assistant Container](https://www.home-assistant.io/faq/ha-vs-hassio/).
Home Assistant OS is the recommended method for almost everyone and includes
Supervisor and Apps. Home Assistant Container has no Supervisor or Apps and
leaves the host, containers, backups, and updates to the user. Older Home
Assistant Core and Supervised installation methods are no longer offered or
supported; they are not distribution targets for Nara Home.

| Home Assistant installation | Recommended Nara package | HA API and configuration access | HA credential | Architectures | Update / uninstall | User difficulty | Real limitations |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Home Assistant OS | Home Assistant App, managed by Supervisor | REST and WebSocket through the internal Supervisor proxy. Mount `homeassistant_config` read-only at an explicit path only when configuration-file reading is enabled; otherwise do not mount it. | Set `homeassistant_api: true`; Supervisor injects `SUPERVISOR_TOKEN`. Nara's App adapter should map the proxy URL and token at runtime, never save the token in App options or logs. | `aarch64`, `amd64` | Install/update/uninstall in Settings > Apps. App image version follows `config.yaml`; backup behavior must be declared and tested as part of the App lifecycle. | Low | Requires HA OS and a separately configured ChatGPT connection. Reading `/config` broadens exposure and remains opt-in/read-only. App packaging and multi-arch images do not exist yet. |
| Home Assistant Container | Independent Nara container in the user's Compose project | Connect to HA REST/WebSocket on a private Docker/LAN network. `/config` is unavailable unless the user explicitly bind-mounts the HA configuration directory read-only; its host path is installation-specific. | Prefer a dedicated HA user and token. Store the token as a Docker secret or equivalently protected local secret and inject it at runtime; never bake it into an image or Compose file. | `linux/amd64`, `linux/arm64` initially | Pull a versioned image and recreate the service; remove the Nara service/image/volume to uninstall. The user owns rollback and host maintenance. | Medium | HA Container does not support Apps. Networking, secret storage, updates, and any optional config bind mount are the user's responsibility. Compose assets and images do not exist yet. |
| Any supported HA installation | Manual Python virtual environment (advanced fallback only) | Connect to REST/WebSocket over the local network. Optional configuration reads require an explicit local read-only mount/path and are normally unavailable from another host. | Dedicated HA token in a local `0600` secret/environment file or a service manager's credential store. | Any 64-bit platform supported by Python 3.11+ and all runtime wheels; official validation targets should still be `amd64` and `arm64`. | Install a versioned wheel in a venv; upgrade or delete that isolated venv/service manually. | High | No lifecycle UI, automatic updates, image reproducibility, Supervisor token, or standard path layout. This is a Nara deployment option, not a Home Assistant installation method. |

The App API mechanism is defined by Home Assistant's
[App communication documentation](https://developers.home-assistant.io/docs/apps/communication/):
the internal REST proxy is `http://supervisor/core/api`, the WebSocket proxy is
`ws://supervisor/core/websocket`, and the injected bearer credential is
`SUPERVISOR_TOKEN`. The official
[App configuration reference](https://developers.home-assistant.io/docs/apps/configuration/)
limits current App architectures to `aarch64` and `amd64`, makes mapped
directories read-only by default, and supports an explicit path for the
`homeassistant_config` mapping. For non-App deployments, Home Assistant's
[authentication API](https://developers.home-assistant.io/docs/auth_api/)
documents user-created long-lived access tokens and requires callers to retain
them securely.

## Connection to ChatGPT

### Recommended private community edition

Each user runs Nara Home inside their own home and creates their own ChatGPT
developer-mode connection. The preferred connection is OpenAI Secure MCP Tunnel;
an HTTPS endpoint owned and authenticated by that user is the fallback. The
Nara process and Home Assistant remain private, and the tunnel client makes only
outbound HTTPS connections.

OpenAI's current
[Secure MCP Tunnel documentation](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
states that the client can reach a private MCP server over either stdio or HTTP,
polls OpenAI over outbound HTTPS, and needs no inbound firewall port. It requires:

- a `tunnel_id` created in Platform tunnel settings;
- a runtime API key for `tunnel-client`;
- local reachability from `tunnel-client` to Nara;
- the Platform permissions Tunnels Read + Use (Read + Manage to create/edit);
- association with the intended Platform organization and ChatGPT workspace;
- ChatGPT developer-mode access, which is a separate account/workspace permission.

Personal accounts use their personal Platform organization. Enterprise/Edu
developer mode is controlled by a workspace administrator. OpenAI explicitly
notes that developer-mode availability can depend on account and workspace
policy, so distribution must not promise availability for every account tier;
the installer must detect or ask the user to verify access.

A tunnel identity, runtime key, organization/workspace association, and
developer-mode connection belong to the user. Nara may later automate local
installation of `tunnel-client`, but it cannot distribute one shared,
preconfigured tunnel. Every user must create and authorize their own tunnel.

For ChatGPT developer-mode connections, the official
[connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt)
accepts either a public HTTPS MCP URL or Secure MCP Tunnel. A public endpoint
must expose MCP Streamable HTTP, normally at `/mcp`; through the secure tunnel,
the private side may be stdio or HTTP. The current public-plugin documentation
does not specify legacy SSE as a supported submission transport, so Nara must
not rely on SSE for this distribution plan.

### Authentication boundaries

The tunnel authenticates its control-plane connection using the user's runtime
key and workspace/organization permissions. If the MCP application itself uses
browser-facing account authorization, OAuth discovery can traverse the tunnel,
but an authorization server is not automatically made reachable by the tunnel.

For a public authenticated MCP server, OpenAI's
[plugin authentication specification](https://developers.openai.com/plugins/build/auth)
requires OAuth 2.1 conforming to the MCP authorization specification, including
protected-resource and authorization-server metadata, authorization code with
PKCE, audience/resource and scope validation, and bearer-token verification.
ChatGPT does not support custom API keys or machine-to-machine grants as a
substitute. Although tools can advertise `noauth`, Home Assistant household data
is user-specific and must not be exposed anonymously.

### Cloudflare alternative

Cloudflare Tunnel or an equivalent reverse proxy is an advanced, user-operated
alternative for someone who already owns a domain and can configure HTTPS,
access control, secret rotation, and origin restrictions. It is not the default,
is not required by the recommended architecture, and is not configured by this
repository.

### Public ChatGPT plugin

A public plugin is a different product and cannot be implemented as a shared
Secure MCP Tunnel. OpenAI requires a stable, publicly reachable HTTPS endpoint
with MCP Streamable HTTP; Secure MCP Tunnel is for developer-mode/private use
and does not satisfy public submission. The endpoint must remain available for
review and domain verification and preserve authentication boundaries.

The official
[plugin submission requirements](https://developers.openai.com/plugins/deploy/submission)
also require a verified developer or business identity, public website/support/
privacy/terms URLs, accurate tool annotations, test cases, reviewer credentials
when authentication is used, a verified MCP domain, and operationally reachable
review infrastructure. A universal server URL is the normal path; per-workspace
template URLs require prior OpenAI approval for trusted developers. Therefore a
public plugin would require infrastructure and OAuth operated by the Nara Home
publisher, plus monitoring, incident response, data-handling policy, and a safe
way to reach each user's home. That conflicts with the initial no-operated-server
decision and is deferred.

## Initial architecture decision

Use one unchanged read-only Python engine and retain its 43-tool contract:

1. Home Assistant OS: package the engine as a Home Assistant App using the
   internal HA API proxy and injected `SUPERVISOR_TOKEN`; do not map HA config by
   default.
2. Home Assistant Container: package the same engine as a multi-architecture
   Compose sidecar using a dedicated HA credential supplied as a local secret.
3. Run `tunnel-client` next to Nara and let each user bind their own Secure MCP
   Tunnel to their own ChatGPT developer-mode connection.
4. Keep manual wheel/venv installation documented only as an advanced fallback.

This design requires no Nara-operated server, keeps Home Assistant and Nara in
the user's network, opens no inbound port, and reuses the current tool catalog.
Tool results necessarily travel to the OpenAI product when the user invokes a
tool; "data stays in the house" therefore means no publisher-operated relay or
database and no unsolicited ingress, not that requested results never leave the
home.

## Blocks before implementation

- Secure MCP Tunnel access is account/workspace- and Platform-permission-dependent.
- App metadata, read-only mount policy, Supervisor-token adapter, and
  `aarch64`/`amd64` images have not been built or validated.
- Compose secret ingestion, health checks, versioned multi-arch image, and
  upgrades/rollback have not been built or validated.
- The tunnel client must be installed, provisioned, and kept running per user;
  its credentials cannot be shared in the distribution.
- A public plugin would require stable publisher-operated HTTPS and OAuth plus
  review, policy, support, monitoring, and domain-verification work. It is not an
  initial distribution target.

## Phase 5D scope

Build only the Home Assistant App packaging skeleton around the current wheel:
`config.yaml`, options/schema, protected container image, startup adapter for
the Supervisor REST/WebSocket proxy and `SUPERVISOR_TOKEN`, default-denied
configuration mount, health check, and `amd64`/`aarch64` local build tests. Do
not add Docker Compose, Secure MCP Tunnel automation, Cloudflare, OAuth, or a
public endpoint in that phase.
