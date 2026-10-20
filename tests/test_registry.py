import json

import httpx
import pytest
from pydantic import Field, SecretStr, ValidationError
from test_service import FakeProvider

from search_router.config import Settings
from search_router.contracts import SearchRequest
from search_router.providers.config import CredentialConfig
from search_router.providers.keys import KeyPool
from search_router.providers.registry import ProviderRegistry, ProviderSpec, builtin_registry
from search_router.search.router import Router


class ExampleConfig(CredentialConfig):
    region: str = "global"
    token: SecretStr | None = Field(default=None, exclude=True, repr=False)


class ExampleProvider(FakeProvider):
    def __init__(self, config, client, api_key=None, timeout=10):
        super().__init__("example")
        self.config = config
        self.api_key = api_key

    async def discover(self):
        return {"region": self.config.region, "credential": self.api_key.get_secret_value()}

    async def quota_status(self):
        from search_router.contracts import QuotaObservation

        return QuotaObservation(remaining=12, unit="credits")


def example_registry():
    return ProviderRegistry(
        [
            *builtin_registry().specs.values(),
            ProviderSpec("example", ExampleConfig, ExampleProvider),
        ]
    )


def test_registry_rejects_duplicates_and_unknown_configuration():
    spec = ProviderSpec("example", ExampleConfig, ExampleProvider)
    with pytest.raises(ValueError, match="duplicate"):
        ProviderRegistry([spec, spec])
    with pytest.raises(ValidationError, match="unknown provider"):
        Settings(providers={"missing": {}})


def test_runtime_settings_ignore_environment_and_deduplicate_stored_keys(monkeypatch):
    monkeypatch.setenv("ROUTER_PROVIDERS", json.dumps({"tavily": {"depth": "advanced"}}))
    monkeypatch.setenv("ROUTER_PROVIDERS__TAVILY__DEPTH", "fast")
    monkeypatch.setenv(
        "ROUTER_PROVIDERS__TAVILY__KEYS", '[" first-key ", "second-key", "first-key"]'
    )
    settings = Settings(
        providers={
            "tavily": {"depth": "advanced", "keys": [" first-key ", "second-key", "first-key"]}
        }
    )
    assert settings.providers["tavily"].depth == "advanced"
    assert [key.get_secret_value() for key in settings.providers["tavily"].credentials()] == [
        "first-key",
        "second-key",
    ]
    assert all(secret not in repr(settings) for secret in ("first-key", "second-key"))


def test_invalid_provider_errors_do_not_include_credentials():
    secret = "credential-that-must-never-appear"
    with pytest.raises(ValidationError) as failure:
        Settings(providers={"tavily": {"keys": [secret], "depth": secret}})
    assert secret not in str(failure.value)
    assert secret not in repr(failure.value)


def test_runtime_settings_ignore_unrelated_environment_values(monkeypatch):
    monkeypatch.setenv("ROUTER_UNKNOWN_SETTING", "private-input")
    assert "private-input" not in repr(Settings())


async def test_registration_constructs_new_provider_without_core_changes():
    registry = example_registry()
    settings = Settings(
        registry=registry,
        provider_order=["example"],
        profiles={"custom": ["example"]},
        providers={
            "example": {
                "enabled": True,
                "keys": ["one", "two"],
                "region": "eu",
                "token": "secret-token",
            }
        },
    )
    async with httpx.AsyncClient() as client:
        provider = registry.build("example", settings, client)
        assert isinstance(provider, KeyPool)
        assert [child.api_key.get_secret_value() for child in provider.providers] == ["one", "two"]
        router = Router(settings, [provider])
        response = await router.search(SearchRequest(query="market"), "scope")
        assert response.status == "empty"
        assert response.usage.upstream_attempts == router.budget.attempts == 1
        assert router.redact("one two secret-token") == "[REDACTED] [REDACTED] [REDACTED]"
    different = settings.model_copy(
        update={
            "providers": registry.configurations(
                {
                    "example": {"enabled": True, "region": "us"},
                }
            )
        }
    )
    assert Router(different, []).policy_fingerprint != router.policy_fingerprint


def test_new_registered_provider_is_disabled_by_default():
    settings = Settings(registry=example_registry())
    assert not settings.providers["example"].enabled
    assert "example" not in settings.provider_order


async def test_registered_provider_management_has_rest_mcp_parity():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from search_router.api.app import create_app
    from search_router.contracts import QuotaObservation

    token = "test-router-token-at-least-24-characters"
    settings = Settings(
        registry=example_registry(),
        tokens={"test": token},
        provider_order=["example"],
        providers={"example": {"enabled": True, "keys": ["private-example-key"], "region": "eu"}},
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        app.state.router.providers["example"].observe_quota(
            0, QuotaObservation(remaining=12, unit="credits")
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + token},
        ) as client:
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                reader,
                writer,
                _,
            ):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    for path, tool, arguments, method, expected_status in [
                        ("/providers", "get_providers", {}, "GET", 200),
                        ("/providers/example", "get_provider", {"name": "example"}, "GET", 200),
                        (
                            "/providers/example/quotas",
                            "get_provider_quotas",
                            {"name": "example"},
                            "GET",
                            200,
                        ),
                        (
                            "/providers/example/discover",
                            "discover_provider",
                            {"name": "example"},
                            "POST",
                            200,
                        ),
                        ("/providers/missing", "get_provider", {"name": "missing"}, "GET", 404),
                        (
                            "/providers/tavily/discover",
                            "discover_provider",
                            {"name": "tavily"},
                            "POST",
                            409,
                        ),
                    ]:
                        rest = await client.request(method, path)
                        result = await session.call_tool(tool, arguments)
                        assert rest.status_code == expected_status
                        assert not result.isError
                        assert result.structuredContent == rest.json()
                        assert "private-example-key" not in rest.text
                    failure = await client.post(
                        "/search", json={"query": "market", "profile": "missing"}
                    )
                    result = await session.call_tool(
                        "search_web", {"request": {"query": "market", "profile": "missing"}}
                    )
                    assert failure.status_code == 422
                    assert result.structuredContent == failure.json()
            assert app.state.router.budget.attempts == app.state.quotas.probe_attempts == 0
