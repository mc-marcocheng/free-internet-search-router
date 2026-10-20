# Contributing to Free Internet Search Router

The goal is dependable internet search for developing agents using limited free-tier provider capacity. Contributions should improve useful availability, constraint handling and operational clarity without bypassing provider terms or limits.

## Development Setup

Use Python 3.11 or newer, uv, and Node.js 22.12+ with npm. Shell examples use Bash; commands without shell-specific syntax also work in PowerShell 7.3+. From the repository root:

```bash
uv sync --locked --extra dev
uv run --locked --extra dev pre-commit install
npm --prefix src/search_router/reader/node ci --ignore-scripts
```

Install the hook once per clone. It runs Ruff lint fixes before formatting staged Python files in `src`, `tests`, and `examples`, using the version in `uv.lock`. Hooks use isolated uv environments so they do not replace a running router executable. If a hook changes files, review and stage those changes, then retry the commit. Unfixable lint errors block the commit. Run all tracked Python files through the hooks with `uv run --locked --extra dev pre-commit run --all-files`; CI still checks the entire project independently.

The deterministic tests do not need provider keys, a running SearXNG service or a downloaded browser. To run the real application and browser reader, follow the [quick start](README.md#quick-start). Keep development databases and credentials outside the repository. Do not read or rewrite another developer's private `.env`.

## Architecture and Contracts

- `src/search_router/contracts.py` owns shared Pydantic REST/MCP contracts.
- `application.py` exposes shared operations; `api/` handles transport and authentication.
- `runtime.py` owns resources; `search/` owns fallback, acceptance, caching and budgets.
- `providers/` contains typed adapters, registration, key pools and quota tracking.
- `reader/` owns public-only acquisition and conversion; `reader/node/` owns Node assets.
- `storage.py`, `protected.py`, and management modules own encrypted settings and credentials.
- `api/web/` is the static management UI; no frontend build step is required.

Keep REST and MCP on the same contracts and service behavior. Preserve user constraints, attribution, explicit unknowns and atomic upstream accounting. An adapter must charge immediately before each actual upstream search, not hide retries behind a single charge. See [Adding a provider](docs/providers.md) for the extension workflow.

Use the official MCP SDK v1 API within the declared dependency range. Reuse the existing native SearXNG HTTP service on port 9090; do not add another container or STDIO server. The router itself is a single-worker service, not a distributed quota store.

## Validation

Run focused tests for your changes, then all repository gates:

```bash
uv run --extra dev pytest -q
uv run ruff check src tests examples
uv run ruff format --check src tests examples
npm --prefix src/search_router/reader/node test
uv build
```

CI targets standard CPython 3.11, 3.12, 3.13, 3.14 and 3.15 on Linux and Windows. The runtime minimum stays 3.11: do not introduce newer-only syntax or APIs. Results from a prerelease interpreter are provisional, not final-release certification. Reproduce a version-specific issue in a separate environment rather than replacing another developer's active environment. Dependency changes must include a regenerated `uv.lock`; Node dependency changes must include the npm lockfile.

Mock HTTP with HTTPX `MockTransport` and MCP with fake sessions. Extend neighboring tests for successful responses, invalid inputs, unknown fields, failures, constraints, redaction and call accounting. UI changes should be checked at desktop and narrow mobile widths, including dialogs, keyboard access and icon tooltips.

## Live Checks

Live requests are not part of routine development or CI. Obtain explicit permission and agree on a call ceiling and conservative estimated spending ceiling first. Never add credentials to tests, fixtures, screenshots, issue reports or logs.

Prefer the UI's selected-key test or the running server's CLI operation:

```bash
uv run search-router key test tavily 1 --confirm-live --max-cost-usd 0.02
```

This targets one key with at most one search, no fallback, and shared server budgets. It refuses unknown saved cost estimates. Estimates are not actual billing guarantees; configure upstream spend caps too. A quota refresh is a separate live account request. See [key tests and quota](docs/configuration.md#key-tests-and-quota).

The optional smoke test uses the protected local configuration and permits at most one actual search call. Set a known conservative estimate for the selected provider before opting in. This Bash-specific example opts into Tavily only for this command, without changing the parent shell's environment:

```bash
ROUTER_LIVE_SMOKE=1 ROUTER_LIVE_PROVIDER=tavily ROUTER_LIVE_MAX_COST_USD=0.02 uv run --extra dev pytest -q tests/test_live.py
```

The standalone smoke test has its own one-call budget, not the running server's accounting. Do not repeatedly invoke it as a way around server limits. A real reader check must likewise use an explicitly approved public URL and bounded output; browser subrequests are additional network activity even though they are not search calls.

## Pull Requests and Security

Keep changes focused. Include the problem, behavioral changes, tests run and any unverified platforms or live behavior. Update relevant docs and examples when contracts, configuration or setup change. Use the name **Free Internet Search Router**; the package, CLI and import identifiers are intentionally unchanged.

Never log or commit credentials, upstream request URLs, request bodies, queries or raw upstream errors. Use sanitized reason codes. Do not weaken reader SSRF controls, browser sandboxing, TLS validation, authentication or protected credential storage. Treat downloaded pages and search text as untrusted data, not agent instructions. Report security issues privately through GitHub's private vulnerability reporting when available; do not disclose exploitable details or secrets in public issues.
