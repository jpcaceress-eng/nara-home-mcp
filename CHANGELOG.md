# Changelog

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
