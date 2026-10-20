# Free Internet Search Router

Give developing agents internet search capability without coupling every project
to individual search vendors. Free-trial APIs have small quotas, so one exhausted
provider can interrupt development even when other providers still have capacity.

Free Internet Search Router puts your configured providers and API keys behind one
authenticated REST API and Streamable HTTP MCP server. It reuses cached searches,
tracks available quota and cooldowns, and falls back through
**SearXNG -> Exa -> SerpAPI -> Tavily** by default. Agents keep the same interface
as providers change or quotas run out. Routing order and key selection are configurable.

"Free" means making the most of available free-tier capacity, not unlimited searches
or a guarantee of zero upstream charges. You supply legitimate provider access;
provider terms, shared quotas, rate limits and any paid usage still apply. The router
does not bypass those limits or create additional trial accounts.

Document reading is independent of search. HTML pages are fetched with HTTPX or
CloakBrowser and converted to Markdown with mdream; text and PDF sources are also supported.

## Quick Start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Node.js 22.12+ with npm.
CI targets Python 3.11 through 3.15 on Linux and Windows. Commands below use PowerShell.
The package remains `internet-search-router`, the import is `search_router`, and
the CLI command is `search-router`.

```powershell
uv sync --locked

Push-Location src/search_router/reader/node
npm ci --ignore-scripts
npm run install-browser
Pop-Location

uv run search-router init
```

Enter and confirm a private administrator token of at least 24 characters. Settings
are stored in SQLite; credentials are encrypted using a master key in the OS credential
vault. `.env` is not loaded. Configure providers through the UI or CLI. SearXNG uses
the existing native service at `http://localhost:9090`, with JSON output enabled.

```powershell
uv run search-router serve
```

Open **http://127.0.0.1:9092/admin** and sign in with your administrator token.
REST uses the same host; MCP is at `/mcp`. Clients send `Authorization: Bearer <token>`.
Settings activate automatically after in-flight operations finish. Host allowlist and
telemetry changes require a restart. Headless deployments use an external protected
key file; see the [configuration guide](docs/configuration.md).

```powershell
uv run search-router config show
uv run search-router config set providers.tavily.depth '"advanced"'
uv run search-router key add tavily
uv run search-router token add local-client
```

Credential commands prompt without echo; secrets are never command-line arguments
or returned in full by the UI. Only named administrators can access management endpoints.

## Use It

| Interface | Search | Read a document |
| --- | --- | --- |
| REST | `POST /search` | `POST /read` |
| MCP | `search_web` | `read_url` |

REST and MCP share request/response contracts. Inspect `status`, `warnings`, and
search `attempts`, not HTTP status alone. Search snippets are discovery material;
read the source URL when you need document text. Markdown may retain related links.

- [Usage](docs/usage.md): REST examples, MCP setup, responses, and available endpoints.
- [Configuration](docs/configuration.md): providers, browser reads, limits, and troubleshooting.
- [Adding providers](docs/providers.md): adapter contracts, registration, and tests.
- [Agent integration](docs/agent-integration.md): instructions for a coding agent connecting another project.
- [Contributing](CONTRIBUTING.md): development setup, architecture, tests, and pull requests.
- [Example client](examples/client.py).

## Development

```powershell
uv run --extra dev pytest -q
uv run ruff check src tests examples
uv run ruff format --check src tests examples
npm --prefix src/search_router/reader/node test
```

Default tests use fake upstreams; live checks require explicit opt-in and bounded
calls/costs. Use the UI or CLI's selected-key checks rather than standalone check
scripts. See [Contributing](CONTRIBUTING.md#live-checks) before making live requests.