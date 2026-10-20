# Free Internet Search Router

Internet search APIs often have limited free-trial quotas, which a research agent can quickly exhaust.

Free Internet Search Router lets you configure multiple search providers, including SearXNG, SerpAPI, Tavily, and Exa. It pools your configured API keys behind a single endpoint, helping your agent keep searching when an individual key reaches its quota. You can add multiple keys for each supported API provider, subject to its account and usage policies. Total search capacity still depends on the providers' quotas.

The router also supports reading web pages and PDFs. HTML pages are fetched with HTTPX or [CloakBrowser](https://github.com/CloakHQ/cloakbrowser) and converted to Markdown with [mdream](https://github.com/harlan-zw/mdream).

## Quick Start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Node.js 22.12+ with npm. CI targets Python 3.11 through 3.15 on Linux and Windows. Commands below work in Bash and PowerShell 7.3+. The package name is `internet-search-router`, the import is `search_router`, and the CLI command is `search-router`.

```bash
uv sync --locked

npm --prefix src/search_router/reader/node ci --ignore-scripts
npm --prefix src/search_router/reader/node run install-browser

uv run search-router init
```

Enter and confirm a private administrator token of at least 24 characters. Settings are stored in SQLite; credentials are encrypted using a master key in the OS credential vault. `.env` is not loaded. Configure providers through the UI or CLI. SearXNG uses the existing native service at `http://localhost:9090`, with JSON output enabled.

```bash
uv run search-router serve
```

Open **http://127.0.0.1:9092/admin** and sign in with your administrator token. REST uses the same host; MCP is at `/mcp`. Clients send `Authorization: Bearer <token>`. Settings take effect automatically after in-flight operations finish. Host allowlist and telemetry changes require a restart. Headless deployments use an external protected key file; see the [configuration guide](docs/configuration.md).

```bash
uv run search-router config show
uv run search-router config set providers.tavily.depth '"advanced"'
uv run search-router key add tavily
uv run search-router token add local-client
```

Credential commands prompt for secrets without displaying them; secrets are never passed as command-line arguments or returned in full by the UI. Only named administrators can access management endpoints.

## Use It

| Interface | Search         | Read a document |
| --------- | -------------- | --------------- |
| REST      | `POST /search` | `POST /read`    |
| MCP       | `search_web`   | `read_url`      |

REST and MCP share request/response contracts. Inspect `status`, `warnings`, and search `attempts`, not the HTTP status code alone. Search snippets are discovery material; read the source URL when you need document text. Markdown may retain related links.

- [Usage](docs/usage.md): REST examples, MCP setup, responses, and available endpoints.
- [Configuration](docs/configuration.md): providers, browser reads, limits, and troubleshooting.
- [Adding providers](docs/providers.md): adapter contracts, registration, and tests.
- [Agent integration](docs/agent-integration.md): instructions for a coding agent connecting another project.
- [Contributing](CONTRIBUTING.md): development setup, architecture, tests, and pull requests.
- [Example client](examples/client.py).
