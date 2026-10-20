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


@pytest.mark.parametrize(
    "locale,expected",
    [
        ("en", ("en", None, None)),
        ("EN-us", ("en", None, "US")),
        ("zh-Hant", ("zh", "Hant", None)),
        ("zh-TW", ("zh", "Hant", "TW")),
        ("zh-Hant-TW", ("zh", "Hant", "TW")),
        ("fr", ("fr", None, None)),
    ],
)
def test_locale_parts_normalizes_without_inventing_country(locale, expected):
    from search_router.providers.http import locale_parts

    request = SearchRequest(query="market", locale=locale)
    assert locale_parts(request) == expected
    assert request.locale == locale


@pytest.mark.parametrize(
    "kind,locale,reason",
    [
        (kind, locale, "invalid_locale")
        for kind in ("google", "bing", "exa", "tavily")
        for locale in ("und", "xx-US", "en-XX")
    ]
    + [
        ("google", "sr-Latn", "script_not_supported"),
        ("bing", "fr", "market_not_supported"),
        ("bing", "fr-CA", "market_not_supported"),
        ("bing", "zh-Hans-TW", "invalid_locale"),
        ("tavily", "en-US", "regional_locale_not_supported"),
        ("tavily", "zh-Hant-HK", "regional_locale_not_supported"),
        ("tavily", "sr-Latn", "regional_locale_not_supported"),
    ],
)
async def test_invalid_or_unsupported_locales_do_not_charge(monkeypatch, kind, locale, reason):
    def forbidden_upstream(request):
        pytest.fail("invalid or unsupported locales must not contact the upstream")

    async def forbidden_charge():
        pytest.fail("invalid or unsupported locales must not charge a search")

    async def forbidden_session(operation):
        pytest.fail("invalid locales must not open an upstream MCP session")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_upstream)) as client:
        name = "serpapi" if kind in {"google", "bing"} else kind
        configuration = {"keys": ["placeholder"]}
        if name == "serpapi":
            configuration["engine"] = kind
        settings = Settings(providers={name: configuration})
        if name == "serpapi":
            provider = SerpAPIProvider(settings.providers[name], client)
        elif name == "tavily":
            provider = TavilyProvider(settings.providers[name], client)
        else:
            provider = ExaProvider(settings.providers[name], client)
            monkeypatch.setattr(provider, "_session", forbidden_session)
        with pytest.raises(ProviderError) as failure:
            await provider.search(SearchRequest(query="market", locale=locale), forbidden_charge)
    assert [issue.model_dump() for issue in failure.value.diagnostics] == [
        {"field": "locale", "value": locale, "reason": reason}
    ]


@pytest.mark.parametrize(
    "engine,locale,expected",
    [
        ("google", "en-US", {"hl": "en", "lr": "lang_en", "gl": "us"}),
        ("google", "EN-us", {"hl": "en", "lr": "lang_en", "gl": "us"}),
        ("google", "zh-Hant-TW", {"hl": "zh-TW", "lr": "lang_zh-TW", "gl": "tw"}),
        ("google", "zh-Hant", {"hl": "zh-TW", "lr": "lang_zh-TW"}),
        ("google", "zh-Hans-CN", {"hl": "zh-CN", "lr": "lang_zh-CN", "gl": "cn"}),
        ("bing", "zh-Hant-TW", {"mkt": "zh-TW"}),
        ("bing", "zh-Hant-HK", {"mkt": "zh-HK"}),
        ("bing", "EN-us", {"mkt": "en-US"}),
        ("bing", "en", {"mkt": "en-US"}),
        ("bing", "zh-Hant", {"mkt": "zh-TW"}),
    ],
)
async def test_serpapi_normalized_locale_mapping(engine, locale, expected):
    calls = []

    async def reserve():
        calls.append(1)

    def upstream(request):
        assert calls == [1]
        for name, value in expected.items():
            assert request.url.params[name] == value
        if engine == "google" and "gl" not in expected:
            assert "gl" not in request.url.params
        return httpx.Response(200, json={"organic_results": []})

    settings = Settings(providers={"serpapi": {"keys": ["placeholder"], "engine": engine}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        await SerpAPIProvider(settings.providers["serpapi"], client).search(
            SearchRequest(query="market", locale=locale), reserve
        )
    assert calls == [1]


@pytest.mark.parametrize("locale", ["zh-TW", "zh-Hant-TW", "ZH-hant-tw"])
async def test_tavily_mapping(locale):
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
                locale=locale,
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
        SearchRequest(query="market", category="news", time_range="day", locale="zh-TW")
    )
    assert name == "web_search_advanced_exa"
    assert args["category"] == "news"
    assert args["userLocation"] == "TW"
    assert "startPublishedDate" in args
    with pytest.raises(ProviderError) as failure:
        provider.arguments(SearchRequest(query="market", locale="en"))
    assert [issue.model_dump() for issue in failure.value.diagnostics] == [
        {"field": "locale", "value": "en", "reason": "country_region_required"}
    ]


@pytest.mark.parametrize(
    "locale,country",
    [
        ("en-US", "US"),
        ("en-GB", "GB"),
        ("zh-TW", "TW"),
        ("zh-Hant-TW", "TW"),
        ("pt-BR", "BR"),
        ("fr-ca", "CA"),
    ],
)
def test_exa_regional_locale_uses_user_location(locale, country):
    provider = ExaProvider(Settings(providers={"exa": {"keyless": True}}).providers["exa"])
    name, args = provider.arguments(SearchRequest(query="market", locale=locale))
    assert name == "web_search_advanced_exa"
    assert args["userLocation"] == country
    assert "locale" not in args
    assert provider.capabilities().locales is None
    assert "locale_country_bias_only" in provider.capabilities().diagnostics


@pytest.mark.parametrize(
    "locale,reason",
    [
        ("en", "country_region_required"),
        ("fr", "country_region_required"),
        ("zh-Hant", "country_region_required"),
        ("en-USA", "invalid_locale"),
        ("en-US-GB", "invalid_locale"),
    ],
)
async def test_exa_locale_without_country_is_rejected_before_session(monkeypatch, locale, reason):
    provider = ExaProvider(Settings(providers={"exa": {"keyless": True}}).providers["exa"])

    async def forbidden_session(operation):
        pytest.fail("a locale without a country must not open an upstream session")

    monkeypatch.setattr(provider, "_session", forbidden_session)
    with pytest.raises(ProviderError) as failure:
        await provider.search(SearchRequest(query="market", locale=locale), charge)
    assert [issue.model_dump() for issue in failure.value.diagnostics] == [
        {"field": "locale", "value": locale, "reason": reason}
    ]


def test_exa_locale_preserves_domain_and_date_constraints():
    provider = ExaProvider(Settings(providers={"exa": {"keyless": True}}).providers["exa"])
    _, args = provider.arguments(
        SearchRequest(
            query="market",
            locale="en-US",
            include_domains=["example.org"],
            exclude_domains=["example.net"],
            start_date="2026-01-01",
            end_date="2026-01-31",
        )
    )
    assert args["userLocation"] == "US"
    assert args["includeDomains"] == ["example.org"]
    assert args["excludeDomains"] == ["example.net"]
    assert args["startPublishedDate"] == "2026-01-01"
    assert args["endPublishedDate"] == "2026-01-31"


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
    locations = []

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

    @server.tool()
    async def web_search_advanced_exa(
        query: str,
        numResults: int,
        userLocation: str,
        textMaxCharacters: int,
        enableSummary: bool,
    ) -> dict:
        assert textMaxCharacters == 2000
        assert enableSummary is False
        locations.append(userLocation)
        return await web_search_exa(query, numResults)

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
        assert {tool["name"] for tool in discovered["tools"]} == {
            "web_search_exa",
            "web_search_advanced_exa",
        }
        assert provider.capabilities().locales is None
        batches = await asyncio.gather(
            *(provider.search(SearchRequest(query="market"), count_charge) for index in range(3))
        )
        assert all(
            batch.results[0].provider == "exa" and batch.results[0].engines == []
            for batch in batches
        )
        localized = await provider.search(
            SearchRequest(query="market", locale="zh-Hant-TW"), count_charge
        )
        assert localized.results[0].provider == "exa"
        assert "locale_applied_as_country_bias" in localized.warnings
        with pytest.raises(ProviderError):
            await provider.search(SearchRequest(query="market", locale="fr"), count_charge)
    assert len(calls) == len(charged) == 4
    assert locations == ["TW"]


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
        (
            {"locale": "en-US"},
            "userLocation",
            "locale",
            "en-US",
            "parameter_not_advertised",
        ),
        (
            {"locale": "zh-TW"},
            "userLocation",
            "locale",
            "zh-TW",
            "schema_value_not_supported",
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


@pytest.mark.parametrize("updates", [{"category": "news"}, {"locale": "en-US"}])
async def test_exa_missing_advanced_tool_is_diagnostic(monkeypatch, updates):
    provider = ExaProvider(Settings(providers={"exa": {"keyless": True}}).providers["exa"])

    async def session(operation):
        return await operation(None)

    monkeypatch.setattr(provider, "_session", session)
    with pytest.raises(ProviderError) as failure:
        await provider.search(SearchRequest(query="Tencent", **updates), charge)
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
