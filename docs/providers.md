# Adding a Provider

An integration consists of one adapter module, one registration, focused tests and
configuration documentation. No changes to global Settings, Router, key pools,
quota tracking, REST or MCP should be necessary.

## Boundaries

- `contracts.py`: shared request/response models and explicit unknowns.
- `providers/base.py`: `Provider`, `Batch`, `Charge`, `ProviderError`, optional
  `Discoverable`, `QuotaSource`, and `AsyncCloseable` protocols.
- `providers/config.py`: `ProviderConfig` and `CredentialConfig`.
- `providers/registry.py`: explicit built-in registrations and generic construction.
- `providers/http.py`: bounded JSON requests, normalization and unsupported-constraint errors.
- `providers/errors.py`: safe failure classification and retry metadata.
- `application.py`: shared operations and redaction; transports only serialize them.
- `runtime.py`: resource ownership; `search/` owns routing and acceptance policy.
- `reader/`: independent document acquisition/conversion and its Node runtime.

## Adapter Contract

Define a typed `ProviderConfig` subclass, or `CredentialConfig` when using API keys.
Add vendor fields there, not to global Settings. Use `SecretStr` for every secret,
with `Field(exclude=True, repr=False)` for additional credential fields. The inherited
`keys` field already has these protections. Unsupported fields are rejected.

Implement a stable `name`, `capabilities()` and
`async search(request: SearchRequest, charge: Charge) -> Batch`. Constructors accept
`(config, client, api_key=None, timeout=10)` and must not perform I/O or acquire
resources. The client is shared and runtime-owned; never close it in an adapter.
The registry supplies a separate credential for each key-pool child. Do not select
or rotate keys in adapters.

Validate unsupported constraints before `await charge()`. Call `charge` exactly
once immediately before each actual search call. Do not hide retries, pagination
or extra billable calls behind a single charge. Raise classified `ProviderError`
codes, never raw upstream text. Preserve all constraints and provider/engine
attribution. Normalize dates, costs and quotas only when known.

Override the config's `cache_identity()` when request semantics need additional
identity; its output must be deterministic JSON without credential values. Default
identity includes all serialized typed fields. Mark secrets excluded from serialization.
Override `quota_identity()` only for account-boundary settings such as a configurable
endpoint or tenant; do not include search options, costs or enablement. Quota identity
additionally hashes each credential and scope; no plaintext credentials are stored.
Credential configs declare a `quota_unit` class variable (`searches`, `credits`,
`usd`, or `unknown`) for exhaustion observations. Monetary observations must be
finite and explicitly distinguish configured budgets from upstream credit balances.

## Registration

Add one entry inside `builtin_registry()` after importing the adapter/config:

```python
ProviderSpec("new_vendor", NewVendorConfig, NewVendorProvider)
```

New registrations are disabled and absent from routing chains by default.
Enable and select the provider explicitly:

```powershell
uv run search-router config set providers.new_vendor.enabled true
uv run search-router key add new_vendor
uv run search-router config set provider_order '["searxng","new_vendor"]'
```

Provider schemas drive the UI's settings fields automatically. `CredentialConfig`
supplies key-pool management without extra UI or CLI work. Persistence encrypts
all `SecretStr` fields, including excluded fields; configuration views never return
their values. Custom secret fields beyond the standard key pool need a dedicated
write-only management operation, not a plaintext setting or environment variable.

Tests can inject a `ProviderRegistry` into `Settings(registry=...)` without mutating
global state. For a registry containing only test adapters, supply matching
`provider_order` and `profiles` rather than the built-in defaults.

## Optional Operations

Implement `async discover() -> dict` for tool/engine inventory. Return a bounded,
allowlisted structure, not a raw upstream payload. The shared discovery operation
applies timeouts, redaction and a typed `provider`/`details` envelope.

Implement `async quota_status() -> QuotaObservation` only when documented balance
discovery is supported. Do not infer a zero balance or exact reset time. Probes run
through the existing sequential background worker and its TTL/call ceiling.
`GET /providers/{name}/quotas` and the matching MCP tool only read its cache.
Choose `shared` quota scope when keys belong to one quota; do not evade rate limits.

Implement `async aclose()` only for resources the adapter owns. Runtime closes
adapter children once after draining searches and stopping quota work. If a provider
creates resources lazily, keep per-call cleanup in `finally` or an async context manager.

## Tests

Use HTTPX `MockTransport` or fake MCP sessions; no real vendor calls by default.
Cover request mapping, capabilities, constraint rejection before charging, attribution,
normalization, unknown values, error classification, and usage. Test quota discovery
only when supported, including privacy and precision. Reuse key-pool tests for retry,
scope, cooldown, cancellation and atomic budget behavior.

`tests/test_registry.py` proves a fake integration's typed grouped config,
construction, key pooling, redaction, cache identity and REST/MCP management parity.
Run focused tests first, then the full gates in [Contributing](../CONTRIBUTING.md#validation).