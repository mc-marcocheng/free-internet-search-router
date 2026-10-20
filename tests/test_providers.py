import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from search_router.config import Settings
from search_router.contracts import SearchRequest
from search_router.providers import (
    ExaProvider,
    ProviderError,
    SearXNGProvider,
    SerpAPIProvider,
    mcp_payload,
    normalize,
)
from search_router.providers.tavily import TavilyProvider


async def charge():
    return None


async def test_native_searxng_mapping_attribution_and_single_charge():
    from urllib.parse import parse_qs

    calls = []

    async def reserve():
        calls.append(1)

    def upstream(request):
        assert request.url.path == "/search"
        assert request.method == "POST"
        assert not request.url.query
        assert parse_qs(request.content.decode()) == {
            "q": ["market"],
            "format": ["json"],
            "categories": ["news"],
            "language": ["zh-TW"],
            "time_range": ["day"],
        }
        assert "authorization" not in request.headers
        assert calls == [1]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.org",
                        "title": "Market",
                        "content": "Market news",
                        "engines": ["google", "bing"],
                    }
                ],
                "unresponsive_engines": [["duckduckgo", "timeout"]],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = SearXNGProvider(Settings().providers["searxng"], client)
        batch = await provider.search(
            SearchRequest(query="market", category="news", locale="zh-TW", time_range="day"),
            reserve,
        )
        assert batch.results[0].engines == ["google", "bing"]
        assert batch.results[0].provider == "searxng"
        assert batch.warnings == []
        assert calls == [1]


async def test_native_searxng_rejects_explicit_dates_without_a_call():
    async def forbidden_charge():
        pytest.fail("unsupported constraints must not issue a search")

    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderError, match="unsupported_capability"):
            await SearXNGProvider(Settings().providers["searxng"], client).search(
                SearchRequest(query="market", start_date="2026-01-01"), forbidden_charge
            )
        with pytest.raises(ProviderError, match="unsupported_capability"):
            await SearXNGProvider(Settings().providers["searxng"], client).search(
                SearchRequest(query="market", time_range="week"), forbidden_charge
            )


def test_searxng_factory_is_http_only():

    assert isinstance(Settings().registry.build("searxng", Settings(), None), SearXNGProvider)
    assert Settings().providers["searxng"].http_url == "http://localhost:9090"
    assert ExaProvider(Settings().providers["exa"]).name == "exa"


async def test_native_searxng_discovery_has_no_search_charge_or_secret_headers():
    from search_router.api.app import create_app

    def upstream(request):
        assert request.url.path == "/config"
        assert not request.url.query
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "categories": ["general", "news", "images"],
                "engines": [
                    {"name": "google", "enabled": True},
                    {"name": "bing", "enabled": False},
                    {"name": "duckduckgo", "enabled": True},
                ],
                "secret": "must-not-leak",
            },
        )

    settings = Settings(tokens={"test": "a" * 32})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = SearXNGProvider(settings.providers["searxng"], client)
        app = create_app(settings, providers=[provider])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://testserver"
            ) as api:
                response = await api.post(
                    "/providers/searxng/discover",
                    headers={"Authorization": "Bearer " + "a" * 32},
                )
                assert response.status_code == 200
                assert response.json()["details"]["engines"] == ["duckduckgo", "google"]
                assert response.json()["details"]["categories"] == ["general", "news"]
                assert "must-not-leak" not in response.text
                assert app.state.router.budget.attempts == 0
        assert provider.capabilities().engines == ["duckduckgo", "google"]


@pytest.mark.parametrize(
    "status,body,reason",
    [
        (403, b"JSON disabled", "authentication"),
        (429, b"{}", "rate_limited"),
        (200, b"<html>challenge</html>", "malformed_response"),
    ],
)
async def test_native_searxng_http_failures_are_explicit(status, body, reason):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=body))
    ) as client:
        with pytest.raises(ProviderError, match=reason):
            await SearXNGProvider(Settings().providers["searxng"], client).search(
                SearchRequest(query="market"), charge
            )


@pytest.mark.parametrize(
    "url", ["http://localhost:9090?format=json", "http://localhost:9090/#search"]
)
def test_native_searxng_requires_base_url(url):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(providers={"searxng": {"http_url": url}})


@pytest.mark.parametrize(
    "body,status,reason",
    [
        (b"captcha", 200, "malformed_response"),
        (b'{"error":"Invalid API key"}', 200, "authentication"),
        (b'{"error":"quota exhausted"}', 200, "quota_exhausted"),
        (b"{}", 429, "rate_limited"),
        (b"{}", 500, "transport"),
    ],
)
async def test_rest_errors(body, status, reason):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=body))
    ) as client:
        provider = TavilyProvider(
            Settings(providers={"tavily": {"keys": ["placeholder"]}}).providers["tavily"], client
        )
        with pytest.raises(ProviderError) as failure:
            await provider.search(SearchRequest(query="market"), charge)
        assert failure.value.reason == reason


async def test_tavily_mapping():
    import json

    def upstream(request):
        data = json.loads(request.content)
        assert data["language"] == "zh-tw"
        assert data["filter_by_language"] is True
        assert data["time_range"] == "week"
        assert data["include_domains"] == ["example.org"]
        assert data["include_answer"] is False
        return httpx.Response(200, json={"results": [], "usage": {"credits": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        batch = await TavilyProvider(
            Settings(providers={"tavily": {"keys": ["placeholder"]}}).providers["tavily"], client
        ).search(
            SearchRequest(
                query="market",
                locale="zh-TW",
                category="news",
                time_range="week",
                include_domains=["example.org"],
            ),
            charge,
        )
        assert batch.usage.credits == 1


def test_exa_official_text_and_advanced_json():
    text = "Title: Market\nURL: https://example.org\nPublished: N/A\nAuthor: N/A\nHighlights:\nMarket growth"
    result = CallToolResult(content=[TextContent(type="text", text=text)])
    data = mcp_payload(result, "exa", "web_search_exa")
    assert normalize(data, "exa").results[0].published_at is None
    provider = ExaProvider(Settings(providers={"exa": {"keyless": True}}).providers["exa"])
    name, args = provider.arguments(
        SearchRequest(query="market", category="news", time_range="day")
    )
    assert name == "web_search_advanced_exa"
    assert args["category"] == "news"
    assert "startPublishedDate" in args
    with pytest.raises(ProviderError) as failure:
        provider.arguments(SearchRequest(query="market", locale="en"))
    assert [issue.model_dump() for issue in failure.value.diagnostics] == [
        {"field": "locale", "value": "en", "reason": "language_filter_not_supported"}
    ]


async def test_serpapi_account_not_search_health():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"api_key": "never-return", "total_searches_left": 3}
            )
        )
    ) as client:
        provider = SerpAPIProvider(
            Settings(providers={"serpapi": {"keys": ["placeholder"]}}).providers["serpapi"], client
        )
        quota = await provider.quota_status()
        assert quota.remaining == 3
        assert quota.unit == "searches"
        assert quota.scope == "account"
        assert "never-return" not in quota.model_dump_json()


def test_searxng_unresponsive_engines_are_silent():
    batch = normalize(
        {"results": [], "unresponsive_engines": [["google", "timeout"]]},
        "searxng",
        engine_warnings=False,
    )
    assert batch.warnings == []


async def test_outbound_mcp_discovery_auth_and_repeated_concurrent_calls():
    import asyncio

    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    server = FastMCP(
        "fake-upstream",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["testserver"]),
    )
    calls = []

    @server.tool()
    async def web_search_exa(query: str, numResults: int) -> dict:
        calls.append((query, numResults))
        return {
            "results": [
                {
                    "url": "https://example.org",
                    "title": "Market",
                    "content": "Market news",
                }
            ],
        }

    upstream = server.streamable_http_app()

    def client_factory(**kwargs):
        assert kwargs["headers"] == {"x-api-key": "test-upstream-key"}
        return httpx.AsyncClient(transport=httpx.ASGITransport(upstream), **kwargs)

    provider = ExaProvider(
        Settings(
            providers={"exa": {"url": "http://testserver/mcp", "keys": ["test-upstream-key"]}}
        ).providers["exa"],
        client_factory=client_factory,
    )
    charged = []

    async def count_charge():
        charged.append(1)

    async with server.session_manager.run():
        discovered = await provider.discover()
        assert discovered["tools"][0]["name"] == "web_search_exa"
        assert provider.capabilities().locales == []
        batches = await asyncio.gather(
            *(provider.search(SearchRequest(query="market"), count_charge) for index in range(3))
        )
        assert all(
            batch.results[0].provider == "exa" and batch.results[0].engines == []
            for batch in batches
        )
        with pytest.raises(Exception):
            await provider.search(SearchRequest(query="market", locale="fr"), count_charge)
    assert len(calls) == len(charged) == 3


@pytest.mark.parametrize(
    "payload,status,expected",
    [
        (
            {
                "search_metadata": {"status": "Success"},
                "search_information": {"organic_results_state": "Fully empty"},
                "error": "Google hasn't returned any results for this query.",
            },
            200,
            "empty",
        ),
        ({"error": "Your account has run out of searches."}, 429, "quota_exhausted"),
        ({"error": "rate limit exceeded"}, 200, "rate_limited"),
        ({"search_metadata": {"status": "Processing"}}, 200, "upstream_error"),
    ],
)
async def test_serpapi_documented_empty_quota_and_pending(payload, status, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    ) as client:
        provider = SerpAPIProvider(
            Settings(providers={"serpapi": {"keys": ["placeholder"]}}).providers["serpapi"], client
        )
        if expected == "empty":
            assert (
                await provider.search(SearchRequest(query="market"), charge)
            ).upstream_count == 0
        else:
            with pytest.raises(ProviderError) as failure:
                await provider.search(SearchRequest(query="market"), charge)
            assert failure.value.reason == expected


async def test_serpapi_rejects_mismatched_echoed_query():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "search_parameters": {"q": "Tencent"},
                    "organic_results": [],
                },
            )
        )
    ) as client:
        provider = SerpAPIProvider(
            Settings(providers={"serpapi": {"keys": ["placeholder"]}}).providers["serpapi"], client
        )
        with pytest.raises(ProviderError) as failure:
            await provider.search(SearchRequest(query="Tencent annual results"), charge)
    assert failure.value.reason == "query_mismatch"
    assert "Tencent" not in str(failure.value)


@pytest.mark.parametrize(
    "updates,parameter,field,value,reason",
    [
        (
            {"include_domains": ["hkexnews.hk"]},
            "includeDomains",
            "include_domains",
            ["hkexnews.hk"],
            "parameter_not_advertised",
        ),
        (
            {"time_range": "week"},
            "startPublishedDate",
            "time_range",
            "week",
            "parameter_not_advertised",
        ),
        ({"category": "news"}, "category", "category", "news", "schema_value_not_supported"),
        ({}, "query", "query", None, "schema_value_not_supported"),
    ],
)
async def test_exa_schema_skips_identify_constraints_without_query_text(
    monkeypatch, updates, parameter, field, value, reason
):
    from types import SimpleNamespace

    provider = ExaProvider(Settings(providers={"exa": {"keyless": True}}).providers["exa"])
    request = SearchRequest(query="private search terms", **updates)
    tool_name, args = provider.arguments(request)
    properties = {name: {} for name in args}
    if reason == "parameter_not_advertised":
        properties.pop(parameter)
    else:
        properties[parameter] = {"enum": ["different_value"]}

    async def session(operation):
        provider.tools = {
            tool_name: SimpleNamespace(
                inputSchema={
                    "type": "object",
                    "properties": properties,
                }
            )
        }
        return await operation(None)

    async def forbidden_charge():
        pytest.fail("unsupported constraints must not issue a search")

    monkeypatch.setattr(provider, "_session", session)
    with pytest.raises(ProviderError) as failure:
        await provider.search(request, forbidden_charge)
    assert failure.value.reason == "unsupported_capability"
    assert [issue.model_dump() for issue in failure.value.diagnostics] == [
        {
            "field": field,
            "value": value,
            "reason": reason,
        }
    ]
    assert request.query not in str(failure.value.diagnostics)


async def test_exa_missing_advanced_tool_is_diagnostic(monkeypatch):
    provider = ExaProvider(Settings(providers={"exa": {"keyless": True}}).providers["exa"])

    async def session(operation):
        return await operation(None)

    monkeypatch.setattr(provider, "_session", session)
    with pytest.raises(ProviderError) as failure:
        await provider.search(SearchRequest(query="Tencent", category="news"), charge)
    assert [issue.model_dump() for issue in failure.value.diagnostics] == [
        {
            "field": "tool",
            "value": "web_search_advanced_exa",
            "reason": "required_tool_not_advertised",
        }
    ]


async def test_exa_unknown_required_parameter_is_diagnostic(monkeypatch):
    from types import SimpleNamespace

    provider = ExaProvider(Settings(providers={"exa": {"keyless": True}}).providers["exa"])

    async def session(operation):
        provider.tools = {
            "web_search_exa": SimpleNamespace(
                inputSchema={
                    "type": "object",
                    "properties": {"query": {}, "numResults": {}, "region": {}},
                    "required": ["region"],
                }
            )
        }
        return await operation(None)

    async def forbidden_charge():
        pytest.fail("missing required parameters must not issue a search")

    monkeypatch.setattr(provider, "_session", session)
    with pytest.raises(ProviderError) as failure:
        await provider.search(SearchRequest(query="private query"), forbidden_charge)
    assert [issue.model_dump() for issue in failure.value.diagnostics] == [
        {
            "field": "region",
            "value": None,
            "reason": "required_parameter_not_supported",
        }
    ]
