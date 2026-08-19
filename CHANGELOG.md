# Changelog

## 0.4.11 — AppArmor native-extension mapping

- Allows read and executable memory mapping for native Python extension modules
  inside the immutable virtual environment.
- Keeps the virtual environment non-writable and preserves all other AppArmor,
  capability, network, and read-only restrictions.

## 0.4.10 — Fixed nonroot runtime configuration

- Removes the root-owned App options file from the unprivileged startup path.
- Uses fixed safe runtime defaults: INFO logging and internal configuration reads disabled.
- Keeps Supervisor API access disabled and the runtime fixed at UID/GID `999:999`.

## 0.4.9 — Native unprivileged HAOS runtime

- Starts the protected App directly as UID/GID `999:999`, avoiding unavailable
  runtime `SETUID` and `SETGID` capabilities.
- Disables the redundant Docker init wrapper for the S6-based App image.
- Fails closed if the container starts with any unexpected runtime identity.

## 0.4.8 — Idempotent HAOS privilege drop

- Accept the already-applied `999:999` runtime identity without privileged
  syscalls, while retaining the fail-closed root privilege drop.

## 0.4.7 — HAOS runtime permission fix

- Makes the packaged virtual environment explicitly readable and traversable by
  the unprivileged runtime account while keeping it non-writable.
- Allows AppArmor read access only to the virtual environment metadata required
  by Python startup.
- Adds an image-level regression check for the read-only UID/GID 999 runtime.

## 0.4.6 — Public snapshot

- Exposes 43 read-only MCP tools.
- Adds dynamic all-domain state and attribute discovery.
- Adds paginated multi-entity Recorder history, statistics, and Logbook access.
- Preserves registry, automation, trace, dashboard, YAML, and text-file diagnostics.
- Removes all control tools from the current catalog.
- Makes configuration-file access explicit and disabled by default.
- Separates runtime and development dependencies.
- Replaces installation-specific documentation and fixtures with public examples.

Older Git commits are retained but do not describe the current supported behavior.
