# Contributing

Contributions should preserve the read-only security boundary and keep fixtures entirely fictional.

Before submitting a change:

1. Do not include `.env`, tokens, private URLs, internal IP addresses, real entity IDs, logs, databases, backups, or Home Assistant configuration captures.
2. Use `example.invalid` hostnames and clearly fictional entity IDs.
3. Add focused tests for changed behavior.
4. Confirm the catalog contains exactly 43 tools and no control or write operation.
5. Run `compileall`, affected tests, `git diff --check`, and a staged-content secret scan.

Packaging for Home Assistant App and Docker will be developed separately from the reusable MCP engine.
