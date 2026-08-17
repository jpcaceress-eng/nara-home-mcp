# Security Policy

## Supported version

The current V4/4B.6 public snapshot is supported. Historical implementations are retained for provenance but are not supported deployment targets.

## Reporting

Report suspected vulnerabilities privately to the repository maintainer. Do not open a public issue containing tokens, passwords, private URLs, entity data, configuration files, or diagnostic captures.

## Deployment guidance

- Use a dedicated Home Assistant account and the minimum permissions required.
- Store `HA_TOKEN` outside Git and rotate it if disclosure is suspected.
- Keep MCP bound to loopback unless a protected network design requires otherwise.
- Require HTTPS and effective authentication for external access.
- Leave configuration-file reads disabled unless they are needed.
- If configuration reads are enabled, expose only an explicitly configured read-only root.
- Review logs and diagnostics before sharing them; Home Assistant state can itself be sensitive.

The current REST transport is GET-only, the WebSocket transport admits only read commands, and the public catalog contains no control or write tool.
