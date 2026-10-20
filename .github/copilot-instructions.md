# Free Internet Search Router

- Use `uv run --extra dev pytest -q` and `uv run ruff check src tests examples`.
- REST and MCP must use the same Pydantic contracts and Router service.
- Preserve constraints, attribution, explicit unknowns, and atomic upstream accounting.
- Never commit credentials or log upstream URLs, request bodies, queries, or raw errors.
- Default tests are deterministic; live tests require explicit opt-in and a call/cost ceiling.
- Use the official MCP SDK v1 API pinned in pyproject.toml. References:
  https://py.sdk.modelcontextprotocol.io/v1/ and
  https://github.com/modelcontextprotocol/python-sdk/blob/v1.x/docs/server.md .
- Use the existing native SearXNG HTTP service on port 9090. No additional SearXNG container or STDIO server.