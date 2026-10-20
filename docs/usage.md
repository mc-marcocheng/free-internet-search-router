# Using the Router

For instructions aimed at a coding agent integrating another project, see
[Agent integration](agent-integration.md).

Start the server with the [quick start](../README.md#quick-start). These examples use
PowerShell 7 and the example address, `http://127.0.0.1:9092`.

## Authenticate

Use a router token provisioned through the management UI or CLI, not an upstream API key.
The token initializer does not export a client environment variable. Enter the token
at the masked prompt rather than putting it in shell history:

```powershell
$base = 'http://127.0.0.1:9092'
$token = Read-Host 'Router token' -MaskInput
$headers = @{ Authorization = "Bearer $token" }
```

All endpoints require this header except `GET /health/live` and `GET /health/ready`.
Do not put tokens in URLs. The OpenAPI endpoints are protected too.

## Search

```powershell
$body = @{
    query = 'semiconductor earnings'
    category = 'news'
    limit = 5
    time_range = 'month'
} | ConvertTo-Json
$search = Invoke-RestMethod "$base/search" -Method Post -Headers $headers `
    -ContentType application/json -Body $body -SkipHttpErrorCheck
$search | ConvertTo-Json -Depth 10
```

`-SkipHttpErrorCheck` lets you inspect a structured failure response as well as a
successful one. Check its `status` and `warnings` before using results.

| Option | Accepted values |
| --- | --- |
| `query` | Required, 1-2,000 characters; must not be blank |
| `limit` | 1-50; default 10 |
| `category` | `general` (default) or `news` |
| `locale` | Optional language/region, such as `en` or `zh-TW` |
| `include_domains`, `exclude_domains` | Arrays of domain names, not URLs |
| `time_range` | `day`, `week`, `month`, or `year` |
| `start_date`, `end_date` | ISO dates; cannot be combined with `time_range` |
| `profile` | `default` or a configured profile name; defaults include `economy` and `quality` |

Unsupported provider constraints cause a skip, not an unrestricted search. Use
`GET /capabilities` and `attempts[].diagnostics` to understand skipped routes.
Defaults: `economy` uses SearXNG; `quality` tries the full chain. Profiles are configurable.

| Search status | HTTP | Meaning |
| --- | --- | --- |
| `ok` | 200 | Results passed the configured acceptance policy |
| `partial` | 200 | Some usable results, but not all acceptance criteria passed |
| `empty` | 200 | No qualifying results |
| `unavailable` | 503 | The router could not produce an available search response |

Keep result URLs, provider/engine attribution, timestamps, and warnings. Unknown
publication dates or costs remain unknown. `attempts` records routing decisions;
`usage` records this request's upstream accounting; `cache` identifies cached or
coalesced responses. A result snippet is not the complete source document.

## Read a Document

```powershell
$body = @{ url = 'https://example.org'; max_chars = 100000 } | ConvertTo-Json
$document = Invoke-RestMethod "$base/read" -Method Post -Headers $headers `
    -ContentType application/json -Body $body -SkipHttpErrorCheck
$document.status
$document.warnings
$document.text
```

HTML becomes mdream Markdown; plain text and PDFs use their own extractors.
`minimal` defaults to `true`. Set `"minimal": false` in the REST body or MCP
`request` to use standard HTML conversion. Minimal mode falls back to standard
only when its output is blank; nonempty metadata-only output is retained.
This flag does not affect plain-text or PDF extraction, or browser fallback rules.

```json
{"url":"https://example.org","max_chars":100000,"minimal":false}
```

`max_chars` defaults to 100,000 and may be set from 1 to 1,000,000. Check `truncated`
before treating text as complete. Even without truncation, extraction is not a
guarantee of article completeness or visual fidelity. Related links may remain.
The source MIME stays in `content_type`, even when `text` contains Markdown.

Reader statuses are `ok` (200), `unsupported` (422), `blocked` (400), and
`unavailable` (503). Reading is independent of search and accepts only public
HTTP(S) destinations on ports 80/443. See [reader configuration](configuration.md#document-reading).

## Connect an MCP Client

Use Streamable HTTP at `http://127.0.0.1:9092/mcp` with the same bearer header. Start
the router separately; there is no STDIO server. VS Code users can use the included
[MCP configuration](../.vscode/mcp.json), which prompts for the token without storing it.

Tool calls use a nested `request` argument:

```json
{"name":"search_web","arguments":{"request":{"query":"semiconductor earnings","limit":5}}}
```

```json
{"name":"read_url","arguments":{"request":{"url":"https://example.org","max_chars":100000}}}
```

`get_capabilities` and `get_providers` take `{}`. `get_provider`,
`discover_provider`, and `get_provider_quotas` take `{"name":"serpapi"}` (use a
provider advertising the operation). Read `structuredContent` for the shared REST
response contract; business failures remain in its `status`. Management and profile
errors use `{"error":"code","fields":[]}` in both transports. Protocol errors are separate.
If your client exposes only text content, parse the serialized JSON response.

## Reference Endpoints and Client

| Endpoint | Purpose |
| --- | --- |
| `GET /health/live` | Process liveness; no authentication |
| `GET /health/ready` | Started and has a configured search provider; not an upstream probe |
| `GET /capabilities` | Provider constraints and configured profiles |
| `GET /providers` | Typed provider/key status, cached quotas, cooldowns, and probe/search counters |
| `GET /providers/{name}` | Capabilities, health and supported operations for one provider |
| `POST /providers/{name}/discover` | Optional live engine/tool discovery; returns `provider` and `details` |
| `GET /providers/{name}/quotas` | Cached quota observations only; never an inline upstream call |
| `GET /openapi.json` | REST OpenAPI definition |
| `GET /schemas/{name}` | Shared request, response, management and outcome schemas |

Unknown providers return HTTP 404, unsupported optional operations 409, unknown
profiles 422, and unconfigured/unavailable operations 503. Their JSON error codes
are identical over MCP. Search/read fields and status meanings remain unchanged.

The [Python client](../examples/client.py) accepts a base URL and token directly.
Its `from_environment()` method expects `SEARCH_ROUTER_URL` and
`SEARCH_ROUTER_TOKEN`; its fallback URL uses port 9092. It does not load `.env`
automatically. For another project, HTTP or
MCP is sufficient; no import of the router implementation is required.