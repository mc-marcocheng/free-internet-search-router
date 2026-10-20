# Configuration and Troubleshooting

Application settings live in SQLite, not `.env` or provider environment variables. The UI and CLI share typed validation, encrypted credential handling and revision checks. Existing private `.env` files are neither read nor rewritten.

## Bootstrap

Only these environment settings are used by the server:

| Environment variable | Default |
| --- | --- |
| `ROUTER_DATABASE` | `%LOCALAPPDATA%/internet-search-router/config.sqlite3` on Windows; `$XDG_DATA_HOME/internet-search-router/config.sqlite3` or `~/.local/share/internet-search-router/config.sqlite3` elsewhere |
| `ROUTER_HOST` | `127.0.0.1` |
| `ROUTER_PORT` | `9092` |
| `ROUTER_KEY_FILE` | Unset: use the OS credential vault |

`.env` is not loaded automatically. Export bootstrap variables in the service's process environment. `--database` and `--key-file` override their bootstrap values; place these global flags before the CLI command. See [bootstrap.env.example](../bootstrap.env.example).

Credentials are AES-256-GCM encrypted individually in SQLite with random nonces and record-bound authentication. The 32-byte master key lives in Windows Credential Manager, macOS Keychain or a supported Linux desktop credential vault. The application fails closed if that facility is unavailable; it never falls back to plaintext storage. Use the same OS account for initialization, CLI operations and the running service.

For a container/headless host, provision a separate file containing a base64-encoded 32-byte random key and set `ROUTER_KEY_FILE` to its path. Provision it using your secret manager, mount it read-only, and restrict permissions to the service identity (0600 on POSIX; a private ACL on Windows). The application does not create an unprotected key file. Do not put its contents in an environment variable, command argument or SQLite record. Keep it outside the database volume/repository and retain it separately for recovery. Losing it makes stored credentials unrecoverable. Protect the database directory as well: non-secret configuration and credential names are not encrypted.

Back up SQLite using its backup API while running, or copy the database after stopping the service. Restore with the same external key; native-vault entries are keyed by the database's absolute path, so changing that path requires deliberate vault/key provisioning. Master-key rotation is not automated. Disk encryption and restricted backups remain recommended. Local processes running as the service account can decrypt credentials; this is not isolation from that account or host administrators.

## Management

```bash
uv run search-router init
uv run search-router serve
uv run search-router config show
uv run search-router config set providers.serpapi.engine '"bing"'
uv run search-router config set providers.tavily.enabled true
uv run search-router config set hourly_attempt_limit 300
uv run search-router key add tavily
uv run search-router key add tavily --name Research
uv run search-router key replace tavily 1
uv run search-router key rename tavily 1 --name Production
uv run search-router key remove tavily 1
uv run search-router token add local-client
uv run search-router token replace local-client
uv run search-router token remove local-client
```

`config set` takes a dotted path and a JSON value. The examples use Bash-compatible quoting that also works in PowerShell 7.3+ with standard native argument passing; older PowerShell versions may strip embedded JSON quotes. `config apply settings.json` accepts the non-secret `configuration` object from `config show`; its credential metadata/schema wrapper is not part of the input. Use the UI's Advanced view for full JSON editing. Key indexes are one-based; inspect current counts before removing or replacing entries. Secret commands prompt without echo; secret values cannot be supplied as CLI arguments. CLI access is privileged local access through the OS account and key store, not a remote API login.

Open `/admin` for provider controls, routing order, limits, reader settings, masked key pools and token roles. The page/assets are public, but all management API requests require an administrator bearer token. `administrators` lists token names with that role; at least one must remain. Newly added client tokens are not administrators. The UI holds its token in memory only, not cookies or browser storage. Reloading the page requires signing in again. Changing your own token requires signing in again after activation. No API returns complete keys or router tokens. Administrator configuration views include key display names and bounded previews: four characters at each end for keys at least 20 characters long, with the middle masked. Shorter keys and all router tokens stay fully masked. Names and exposed fragments are non-secret metadata; choose labels that do not contain credentials. These previews are not included in ordinary provider status or MCP responses.

Keys can be named when added or replaced, and renamed without changing the secret. Existing unnamed keys receive labels such as `Key 1`. Names survive replacement, configuration edits and restarts. The CLI accepts `--name` on key add/replace and requires it for `key rename`. Names do not affect routing, cache identity or quotas. The UI's help controls explain key-selection strategies, quota scope and limits.

### Key Tests and Quota

Each key row has a test (play) action, a quota-refresh action, and cached status. Neither action runs on page load. Tests require confirmation and a saved conservative `estimated_cost_usd` of at most $1 per attempt. A test makes at most one search with the selected key, without fallback, and uses the server's existing search/cost budgets, deadlines and cooldowns. Its fixed query is `Python official documentation`. Working means an upstream response was received, not a guarantee of result quality or future availability. Estimates cap reserved spending, not the provider's actual bill; actual cost remains unknown unless reported upstream.

Quota reads are cached and make no upstream requests. Refresh explicitly makes one account/quota request, sharing the background worker's global `quota_refresh_hourly_limit`, serialization and failure backoff. It also respects active Retry-After delays. Explicit refresh works when background refresh is disabled. The UI distinguishes unknown, stale, unsupported, exhausted and failed observations; observation times and detailed status are available in tooltips rather than repeated visible timestamps. Reset information is provider-documented unless explicitly identified as an assumed budget boundary. SerpAPI reports searches; Tavily reports account credits, not key limits/usage. Exa reports remaining assumed monthly dollar budget as described below. No reset time is invented from a date-only renewal field.

#### Exa Monthly Budget

Exa Team Management access must be enabled for your team by Exa support. Create a **service account key** in the dashboard's Service keys tab; ordinary search keys cannot authorize usage analytics. Add it with the UI's **Quota service key** control or a non-echoing CLI prompt:

```bash
uv run search-router service-key add exa
uv run search-router config set providers.exa.usage_key_ids '["YOUR-KEY-ID"]'
uv run search-router key refresh-quota exa 1 --confirm-live
uv run search-router key status exa
```

Use `service-key replace exa` or `service-key remove exa` to rotate/remove the protected service credential. It is encrypted like search keys, never returned or previewed, and never sent to Exa MCP. `usage_key_ids` contains the management IDs from Exa's dashboard/API, **not the secret search-key values**, in key-pool order. Removing a key removes its ID; replacing a key clears its ID to prevent attributing another key's usage. Empty IDs and missing service credentials make no probe calls.

Each refresh issues one GET to Exa's documented `https://admin-api.exa.ai/team-management/api-keys/{id}/usage`, with explicit UTC calendar-month-start and current-time parameters. The default assumed budget is **USD 10 per key per UTC calendar month**, configurable with `providers.exa.monthly_budget_usd`. Remaining budget is `max(0, budget - total_cost_usd)`; it is not Exa's free-credit balance, a team-wide shared budget, or its actual billing period. The result identifies `source=configured_budget`, `unit=usd`, reported spend, and the assumed budget. Its reset is the next UTC calendar month, not a provider reset claim. Observed exhaustion follows the existing key cooldown rules; this is cached monitoring, not a transactionally enforced monthly billing cap. Missing, invalid, wrong-key or mismatched-period reports never become a fresh $10 balance. Refresh uses the same global probe ceiling and backoff as other providers.

The regular provider form hides `usage_key_ids` and `monthly_budget_usd`; configure them through the CLI or Advanced JSON view. The protected service key has its own write-only control and must not be pasted into JSON configuration.

Reference: [Exa Team Management specification](https://exa.ai/docs/team-management-spec.yaml).

The CLI uses the running single-worker server for these operations, so it cannot create a fresh budget per invocation. It retrieves an administrator token from the local protected store, without printing it or taking it on the command line:

```bash
uv run search-router key status tavily
uv run search-router key test tavily 1 --confirm-live --max-cost-usd 0.02
uv run search-router key refresh-quota tavily 1 --confirm-live
```

Use `--url` after the subcommand for another server address; HTTP is restricted to loopback, otherwise HTTPS is required. Redirects and environment proxies are disabled. Search tests fail closed for unknown estimates or an exceeded ceiling; failed or skipped live operations exit nonzero. All key indexes are one-based, and live actions require the active configuration revision. Last-test results are safe in-memory metadata preserved for matching keys across reloads, not durable audit records.

Management endpoints are `GET/PUT /admin/api/configuration` and `POST /admin/api/credentials`, plus cached `GET /admin/api/keys/{provider}` and live `POST /admin/api/key-actions`. Live actions use `{revision, provider, index, action, confirm_live: true, max_cost_usd}`; `action` is `test` or `refresh_quota`, and `max_cost_usd` is required for tests. Writes and live actions require the current `revision`; stale writes return 409 without changing storage. Invalid updates leave the revision unchanged. Credentials have add/replace/remove operations and provider-key rename, not a complete-secret retrieval operation. Management is intentionally HTTP/local CLI only; no secret-writing MCP tools are exposed.

The running single-worker server checks for CLI-written revisions every second. UI saves attempt activation immediately. While activating, new search/read/discovery/key-check operations wait, active operations finish against their original settings, and the runtime is replaced. Hourly search/cost/probe accounting, circuit state and matching credential cooldowns survive configuration reloads. Search-response caches are cleared. Saved and active revisions are reported separately; activation failures leave the previous runtime active and are retried. `allowed_hosts` and `otlp_endpoint` changes are reported as restart-required. Bootstrap changes always require a restart.

## Address and Access

The server binds to `127.0.0.1:9092` by default. Router tokens must be at least 24 characters and cannot contain the placeholder marker. Initialize interactively:

```bash
uv run search-router init
```

Initialization refuses to overwrite an existing database. Token names partition the search cache; only names listed in `administrators` have management privileges. Keep the service on loopback unless you have configured TLS and a trusted reverse proxy. For another hostname, set `allowed_hosts` to a JSON list of allowed hostnames. Browser requests with an `Origin` must match the request host and scheme rules; wildcard CORS access is not provided.

## Search Providers

| Provider | Local settings | Requirements and limitations |
| --- | --- | --- |
| SearXNG | `http_url` | Existing HTTP service, normally `http://localhost:9090`; JSON search output must be enabled |
| Exa | `keys`, `keyless`, `url` | Uses Exa MCP; locale-constrained requests are skipped |
| SerpAPI | `keys`, `engine` | Built-in engine default is `google`; `bing` supports general/Bing News |
| Tavily | `keys`, `depth` | Depth defaults to `basic`; unsupported regional locales are skipped |

These fields belong to provider-specific blocks in the persisted `providers` object. Change non-secret options with the UI or CLI; add keys through protected prompts:

```bash
uv run search-router config set providers.serpapi.engine '"bing"'
uv run search-router key add serpapi
uv run search-router config set providers.tavily.depth '"advanced"'
```

Unknown configuration fields, provider IDs, provider fields and invalid chains are rejected before resources start. Configuration errors omit supplied values. No flat vendor or provider environment settings are supported; use persisted configuration and the dedicated credential commands described above.

Missing optional keys do not prevent startup. Configuration does not prove an upstream service is reachable. SearXNG engine configuration stays in your existing server; the router does not start a service, enable engines, or paginate results.

Limit the chain by setting both fields, for example:

```bash
uv run search-router config set provider_order '["searxng","serpapi"]'
uv run search-router config set providers.exa.enabled false
uv run search-router config set providers.tavily.enabled false
```

`profiles` maps custom names to provider chains; `default` is reserved for `provider_order`. The built-in names are `economy` and `quality`. Use `GET /capabilities` for current constraints, and inspect response diagnostics when a route is skipped. Strict constraints can reduce results: unknown publication dates cannot satisfy strict date filters, and too few relevant contributing engines can prevent SearXNG acceptance. Do not automatically remove constraints to get results.

### Multiple API Keys

Exa, SerpAPI, and Tavily support multiple protected keys; whitespace is removed and duplicate additions are rejected. Configure selection independently:

```bash
uv run search-router key add exa
uv run search-router config set providers.exa.key_strategy '"round_robin"'
uv run search-router config set providers.serpapi.key_strategy '"failover"'
uv run search-router config set providers.tavily.key_strategy '"least_inflight"'
uv run search-router config set key_max_attempts 2
```

`round_robin` rotates keys; `failover` retains a working key after failure. Both can retry eligible authentication/transport/timeout and independent-key quota failures with an untried key within the same deadline and budget. `random` chooses one key without retries; `least_inflight` selects the least busy key. The maximum includes the initial call; set it to 1 to disable retries. Known exhausted/cooling keys are excluded before any strategy selects a key. Provider-wide rate limits always cool down the pool; they never trigger rotation to bypass throttling.

### Quota Discovery and Cooldowns

```json
{
	"key_quota_cooldown_seconds": 3600,
	"quota_cache_ttl_seconds": 900,
	"quota_refresh_enabled": true,
	"quota_refresh_hourly_limit": 60,
	"quota_cache_enabled": true,
	"quota_cache_dir": null
}
```

Keys default to independent quotas. Set a provider's scope to `shared` when its keys share an account/quota: exhaustion then blocks the entire pool without failover. Do not use independent rotation to evade a provider's shared limits.

| Provider | Cached quota discovery | Reset information |
| --- | --- | --- |
| SerpAPI | Remaining account searches from its free account API | Nullable renewal date, not an exact reset time |
| Tavily | Remaining account credits; key-level limits are ignored | Not provided by the documented usage API |
| Exa | Assumed monthly budget minus reported spend; requires a separate service key and matching usage key IDs | Next UTC month is an assumed budget boundary, not an upstream reset claim |
| SearXNG | No credential pool or quota probes | Not applicable |

The background worker checks missing/stale supported balances at startup and thereafter at most once per key per TTL (15 minutes by default), subject to a global 60-probe/hour ceiling, sequential execution, timeout, and failure backoff. It does not delay readiness or search requests. Unknown/stale balances allow a search unless confirmed exhaustion or a cooldown blocks it. Failed/unknown refreshes never turn unknown balances into zero or erase confirmed exhaustion. Searches and cached account-status reads never make inline quota-discovery calls.

Exhaustion blocks the affected key for 3,600 seconds when an exact future reset is unavailable. A trusted exact reset takes precedence, with `Retry-After` always respected as a minimum delay. A date-only renewal is displayed without inventing a time or timezone. A fresh positive balance can recover a key earlier, except during a retry minimum. After the block expires, only one recovery search per exhausted key is admitted at a time. `random` and `least_inflight` still make just one attempt per request, but skip known unavailable keys.

Balances are observations, not guaranteed local reservations: external usage can change them, and this version does not predict/decrement balances after each search. Tavily credits are not an exact remaining-search count. Its account balance is `plan_limit - plan_usage` (clamped to zero), plus any explicitly reported finite pay-as-you-go balance. Key-level limits and usage are ignored: account credits alone determine the reported remaining quota. Missing or invalid account figures remain unknown; a null key limit does not affect a known account balance. Quota probes have separate status counters and an hourly ceiling, not search-attempt charges. Only SerpAPI's account API is documented here as free; other probe costs remain unknown. Set the probe ceiling to zero or disable refresh to make no automatic balance calls. Failure backoff starts at up to 60 seconds and grows to the TTL, honoring longer `Retry-After` delays.

The versioned cache is `%LOCALAPPDATA%/internet-search-router/quota-cache.json` on Windows, or `$XDG_CACHE_HOME/internet-search-router/quota-cache.json` (falling back to `~/.cache`) elsewhere. A directory override supports containers and service accounts. Atomic writes contain only quota/cooldown metadata and hashed credential identities, never plaintext keys, queries, account emails, or raw responses. Windows permissions inherit from the user's directory; POSIX cache files are private. Treat the cache as private operational metadata. Corrupt or unwritable storage falls back to memory and is reported as `unavailable` in `GET /providers`. Disabling disk caching retains in-memory cooldowns.

Provider status includes balance units, observation freshness, renewal/reset information, effective per-key cooldowns, and refresh outcomes. Generic `GET /providers/{name}/quotas` returns cached observations without triggering any upstream call. Unsupported operations return `unsupported_operation`. Account authentication does not establish search quality. See the [SerpAPI account API](https://serpapi.com/account-api) and [Tavily usage API](https://docs.tavily.com/documentation/api-reference/endpoint/usage).

## Document Reading

```json
{
	"reader_backends": ["httpx", "cloakbrowser"],
	"reader_timeout": 20,
	"reader_backend_timeout": 12
}
```

Each backend is tried at most once. HTTPX is followed by CloakBrowser on eligible transport failures, timeouts, HTTP 403 responses, server errors, compressed-response limitations, or empty extraction. All attempts and conversion share the overall deadline, so the second backend may have less time than its per-backend allowance.

For JavaScript-dependent pages, choose `["cloakbrowser","httpx"]` for browser-first or `["cloakbrowser"]` for browser-only reads. Use `["httpx"]` to disable browsers; HTML conversion still needs Node and mdream. Keep HTTPX enabled for PDF/plain-text acquisition. HTTPX HTTP 403 responses allow one attempt with the configured browser backend; a browser-side HTTP 403 remains terminal. HTTP 401/429, detected challenge pages, unsafe URLs, and size limits do not trigger automatic browser fallback. No login, CAPTCHA solving, or consent clicking is provided.

Run `npm ci --ignore-scripts` and `npm run install-browser` in `src/search_router/reader/node` during setup. For a non-editable Python installation, run them in the installed `search_router/reader/node` directory instead. Set `reader_node_executable` if Node is not on the service user's PATH. Install the browser as the user who will run the service; requests never download it.

HTML uses the published mdream package and the final URL as origin. The read request's `minimal` parameter defaults to `true`; set it to `false` for standard conversion, identified by `html_to_markdown_mdream_standard`. If minimal output is blank, conversion falls back to standard mdream using the same downloaded HTML; no extra download is required. Responses identify this with `html_to_markdown_mdream_standard_fallback`. Output is unchanged except for requested character truncation and required known-secret redaction. There is no custom recommendation removal or summarization. PDF extraction has no OCR and does not preserve visual layout.

Public-only DNS/IP pinning applies to downloads and browser HTTP(S) traffic. Private destinations are blocked, including browser redirects and subresources. Browser contexts are fresh; images/fonts/media, WebSockets, service workers, and unproxied WebRTC are disabled. This is not an OS sandbox: use deployment network, memory, CPU, and PID controls for untrusted pages.

## Limits and Costs

| Setting                    | Built-in default                        |
| -------------------------- | --------------------------------------- |
| `total_timeout`            | 45 seconds per search                   |
| `attempt_timeout`          | 10 seconds per provider attempt         |
| `max_attempts_per_request` | 4 upstream calls, including key retries |
| `hourly_attempt_limit`     | 1,000 search calls                      |
| `cache_ttl`                | 300 seconds                             |
| `reader_limit_bytes`       | 5,000,000 bytes                         |
| `reader_concurrency`       | 4 reads                                 |
| `reader_max_pages`         | 100 PDF pages                           |

Reader total/per-backend timeouts may be raised to at most 45 seconds. Increasing `max_chars` does not increase the byte limit or remove timeout/page limits. Run one worker. Search budgets, infrastructure circuit breakers, search-response cache, and quota-probe ceilings remain in memory and reset on restart. Credential cooldowns and quota observations survive restarts when quota disk caching is enabled. This does not provide distributed budget or multiworker coordination.

### Containers

Mount a private writable directory at `/data`, owned by UID 10001, and an externally provisioned read-only master-key file at `/run/secrets/router-master.key`. Set `ROUTER_KEY_FILE=/run/secrets/router-master.key`. Run `search-router init` interactively once with the same database volume/key mounts, then start the image's normal command. The image uses `/data/config.sqlite3` and port 9092; publish that port to loopback or a TLS reverse proxy. Configure `allowed_hosts` before remote access and restart. Mount a separate writable quota-cache directory if you need durable cooldowns. Native SearXNG still runs externally on port 9090; use an address reachable from the container, not a second SearXNG service.

Use `policy` for acceptance requirements; `config show` displays its current JSON. Each provider block accepts `hourly_attempt_limit` and `estimated_cost_usd`. Optional `hourly_cost_limit_usd` requires conservative per-provider estimates; unknown estimates fail closed when the ceiling is set. Estimates are not a billing guarantee. Also configure provider-side spend limits.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Startup rejects tokens | Run the initializer, then restart; avoid placeholder or short tokens |
| HTTP 401 | Send the router token in the bearer header, not an upstream key; sign in again after token rotation |
| HTTP 403 / invalid host | Check administrator role, `Origin`, `Host`, reverse-proxy settings, and `allowed_hosts` |
| Readiness returns 503 | No enabled search provider is configured, or startup is incomplete; readiness does not probe upstream health |
| Search is partial/empty/unavailable | Inspect `attempts`, diagnostics, warnings, provider status, constraints, and budgets before retrying |
| `reader_runtime_missing` / `mdream_conversion_failed` | Check Node availability and run the locked npm install in the reader runtime directory |
| `browser_not_installed` | Run `npm run install-browser` as the service user; check Linux sandbox support |
| `read_timeout` | Inspect the overall and per-backend deadlines; consider browser-first for JavaScript pages |
| Reader is blocked | The destination or a browser subresource is unsafe/non-public; do not disable the network protections |
| Markdown contains related articles | Expected mdream output; no article-only guarantee is made |

For an explicit selected-key check, use the running server's bounded CLI operation:

```bash
uv run search-router key test tavily 1 --confirm-live --max-cost-usd 0.02
```

This can incur charges and requires a saved conservative cost estimate. A successful account or discovery check does not establish search quality. For a reader check, make one explicit `POST /read` or `read_url` call using the [usage guide](usage.md), with an approved public URL and bounded `max_chars`. It makes no search calls, but browser acquisition may issue bounded subrequests. The deterministic suite and the separately opted-in provider smoke test are documented in [Contributing](../CONTRIBUTING.md#live-checks).

## Container Deployment

Provision the protected master-key file described in [Bootstrap](#bootstrap) first. Its permissions must allow the container's UID 10001 to read it. Replace `/absolute/path/to/router-master.key` with its absolute host path, not its contents. The following commands work in Bash and PowerShell 7.3+; on Windows, use a host path such as `C:/private/router-master.key`:

```bash
docker build -t free-internet-search-router .
docker run --rm -it --mount type=volume,source=free-search-router-data,target=/data --mount "type=bind,source=/absolute/path/to/router-master.key,target=/run/secrets/router-master.key,readonly" -e ROUTER_KEY_FILE=/run/secrets/router-master.key --entrypoint /app/.venv/bin/search-router free-internet-search-router init
docker run --rm --mount type=volume,source=free-search-router-data,target=/data --mount "type=bind,source=/absolute/path/to/router-master.key,target=/run/secrets/router-master.key,readonly" -e ROUTER_KEY_FILE=/run/secrets/router-master.key -p 127.0.0.1:9092:9092 --memory=2g --cpus=2 --pids-limit=256 free-internet-search-router
```

The image includes Node, reader dependencies, and a browser; it runs only the router. Initialize only once with the same database volume and master-key mounts. Configure provider options and keys through `/admin`, not runtime `.env` files or provider environment variables. Set `providers.searxng.http_url` to an address reachable from the container, such as `http://host.docker.internal:9090`. On Linux, host access may need `--add-host=host.docker.internal:host-gateway` on the serving command and an appropriately bound native service. Do not start another SearXNG service or broadly expose it just to make networking work.

Mount a separate writable quota-cache directory for durable cooldowns across container removal. Chromium needs Linux sandbox/user-namespace support; do not disable its sandbox to work around deployment failures. Size resources for the workload. Keep credentials out of the image, use TLS for remote access, and configure `allowed_hosts` before remote access and restart.
