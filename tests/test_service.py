import asyncio

import pytest
from test_policy import result

from search_router.config import Settings
from search_router.contracts import ProviderCapabilities, SearchRequest
from search_router.providers import Batch, ProviderError
from search_router.search.router import Router


class FakeProvider:
    def __init__(self, name, results=None, error=None, wait=None, configured=True, warnings=None):
        self.name, self.results, self.error = name, results or [], error
        self.wait, self.configured, self.calls = wait, configured, 0
        self.warnings = warnings or []
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    def capabilities(self):
        return ProviderCapabilities(name=self.name, configured=self.configured)

    async def search(self, request, charge):
        await charge()
        self.calls += 1
        self.entered.set()
        try:
            if self.wait:
                await self.wait.wait()
            if self.error:
                raise self.error
            return Batch(self.results, len(self.results), self.warnings)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


async def test_quorum_met_despite_failures_stops_fallback():
    first = FakeProvider(
        "searxng", [result(engines=["google", "bing"])], warnings=["invalid_result"]
    )
    second = FakeProvider("exa", [result()])
    response = await Router(Settings(), [first, second]).search(
        SearchRequest(query="market", limit=1), "scope"
    )
    assert response.status == "ok"
    assert response.selected_provider == "searxng"
    assert second.calls == 0
    assert response.usage.upstream_attempts == 1


async def test_fallback_and_best_partial():
    first = FakeProvider("searxng", [result(engines=["google"])])
    second = FakeProvider("exa", error=ProviderError("transport"))
    router = Router(Settings(), [first, second])
    response = await router.search(SearchRequest(query="market"), "scope")
    assert response.status == "partial"
    assert response.selected_provider == "searxng"
    assert response.attempts[0].reason == "insufficient_engine_quorum"
    second.error, second.results = None, [result()]
    response = await router.search(SearchRequest(query="market"), "scope")
    assert response.status == "ok"
    assert response.selected_provider == "exa"


async def test_empty_not_unavailable_or_circuit_failure():
    provider = FakeProvider("exa")
    router = Router(Settings(circuit_threshold=1, cache_ttl=0), [provider])
    for query in ("market", "other"):
        response = await router.search(SearchRequest(query=query), "scope")
        assert response.status == "empty"
    assert router.health["exa"].blocked_until == 0
    assert provider.calls == 2


async def test_disabled_unconfigured_and_all_failed():
    router = Router(
        Settings(
            providers={
                "searxng": {"enabled": "searxng" in ["exa"]},
                "exa": {"enabled": "exa" in ["exa"]},
                "serpapi": {"enabled": "serpapi" in ["exa"]},
                "tavily": {"enabled": "tavily" in ["exa"]},
            }
        ),
        [FakeProvider("exa", configured=False)],
    )
    response = await router.search(SearchRequest(query="market"), "scope")
    assert response.status == "unavailable"
    assert response.attempts[0].reason == "disabled"
    assert response.attempts[1].reason == "unconfigured"


async def test_cache_expiration_scope_and_constraints():
    clock = [0]
    provider = FakeProvider("exa", [result()])
    router = Router(Settings(), [provider], clock=lambda: clock[0])
    request = SearchRequest(query="market")
    first = await router.search(request, "a")
    second = await router.search(request, "a")
    assert second.cache.hit and first.request_id != second.request_id
    assert second.usage.upstream_attempts == 0
    await router.search(request, "b")
    await router.search(request.model_copy(update={"limit": 1}), "a")
    clock[0] = 301
    await router.search(request, "a")
    assert provider.calls == 4


async def test_singleflight_and_cancellation():
    release = asyncio.Event()
    provider = FakeProvider("exa", [result()], wait=release)
    router = Router(Settings(), [provider])
    request = SearchRequest(query="market")
    first = asyncio.create_task(router.search(request, "scope"))
    await provider.entered.wait()
    second = asyncio.create_task(router.search(request, "scope"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not provider.cancelled.is_set()
    release.set()
    response = await second
    assert response.cache.coalesced
    assert provider.calls == 1
    assert not router.inflight


async def test_last_caller_cancels_upstream():
    provider = FakeProvider("exa", wait=asyncio.Event())
    router = Router(Settings(), [provider])
    task = asyncio.create_task(router.search(SearchRequest(query="market"), "scope"))
    await provider.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled.is_set()
    assert not router.inflight


async def test_concurrent_atomic_budget_and_unknown_cost():
    provider = FakeProvider("exa", [result()])
    router = Router(Settings(hourly_attempt_limit=1), [provider])
    responses = await asyncio.gather(
        *(
            router.search(SearchRequest(query="market " + str(index)), "scope")
            for index in range(10)
        )
    )
    assert provider.calls == 1
    assert sum(response.usage.upstream_attempts for response in responses) == 1
    router = Router(Settings(hourly_cost_limit_usd=1), [provider])
    response = await router.search(SearchRequest(query="market"), "scope")
    assert response.attempts[1].reason == "unknown_cost_budget"


async def test_deadline_and_cooldown():
    provider = FakeProvider("exa", wait=asyncio.Event())
    router = Router(
        Settings(total_timeout=0.02, attempt_timeout=1, circuit_threshold=1), [provider]
    )
    response = await router.search(SearchRequest(query="market"), "scope")
    assert response.status == "unavailable"
    assert response.attempts[1].reason == "timeout"
    response = await router.search(SearchRequest(query="market"), "scope")
    assert response.attempts[1].reason == "cooldown"


async def test_secret_redaction():
    candidate = result()
    candidate.snippet += " very-secret-value"
    provider = FakeProvider("exa", [candidate])
    router = Router(Settings(providers={"exa": {"keys": ["very-secret-value"]}}), [provider])
    response = await router.search(SearchRequest(query="market"), "scope")
    assert "very-secret-value" not in response.model_dump_json()


async def test_wholly_irrelevant_results_are_not_useful_partial():
    response = await Router(Settings(), [FakeProvider("exa", [result()])]).search(
        SearchRequest(query="volcano"), "scope"
    )
    assert response.status == "unavailable"
    assert not response.results
    assert response.attempts[1].reason == "low_quality"


async def test_retry_after_and_auth_differ_from_infrastructure():
    provider = FakeProvider("exa", error=ProviderError("rate_limited", retry_after=200))
    clock = [0]
    router = Router(Settings(circuit_threshold=1), [provider], clock=lambda: clock[0])
    await router.search(SearchRequest(query="market"), "scope")
    assert router.health["exa"].blocked_until == 200
    assert router.health["exa"].failures == 0
    assert router.health["exa"].account_health == "rate_limited"
    clock[0] = 201
    provider.error = ProviderError("authentication")
    await router.search(SearchRequest(query="market"), "scope")
    assert router.health["exa"].account_health == "authentication"
    assert router.health["exa"].failures == 0


async def test_cost_budget_reserves_all_fallback_attempts():
    providers = [
        FakeProvider("searxng", [result(engines=["google"])]),
        FakeProvider("exa", [result()]),
    ]
    router = Router(
        Settings(
            hourly_cost_limit_usd=0.6,
            providers={"searxng": {"estimated_cost_usd": 0.2}, "exa": {"estimated_cost_usd": 0.5}},
        ),
        providers,
    )
    response = await router.search(SearchRequest(query="market"), "scope")
    assert response.status == "partial"
    assert response.attempts[1].reason == "cost_budget_exhausted"
    assert router.budget.reserved_cost == 0.2


async def test_serpapi_distinct_queries_forward_exactly_and_have_separate_cache_entries():
    import httpx

    from search_router.providers import SerpAPIProvider

    queries = [
        "Tencent deferred tax reconciliation segment liabilities",
        "Tencent annual results site:hkexnews.hk",
        'Tencent "annual results" revenue growth',
    ]
    received = []

    def upstream(request):
        query = request.url.params["q"]
        received.append(query)
        assert request.url.params["engine"] == "bing"
        return httpx.Response(
            200,
            json={
                "search_parameters": {"q": query},
                "organic_results": [
                    {
                        "link": "https://www.hkexnews.hk/report.pdf",
                        "title": query,
                        "snippet": "Tencent annual results deferred tax reconciliation segment liabilities",
                    }
                ],
            },
        )

    settings = Settings(
        provider_order=["serpapi"],
        providers={"serpapi": {"keys": ["placeholder"], "engine": "bing"}},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        router = Router(settings, [SerpAPIProvider(settings.providers["serpapi"], client)])
        for query in queries:
            response = await router.search(SearchRequest(query=query), "scope")
            assert response.status == "ok"
            assert response.effective_request.query == query
            assert not response.cache.hit
            assert response.usage.upstream_attempts == 1
        for query in queries:
            response = await router.search(SearchRequest(query=query), "scope")
            assert response.cache.hit
            assert response.effective_request.query == query
            assert response.usage.upstream_attempts == 0
        assert received == queries


async def test_exa_skip_diagnostics_survive_key_pool_and_preserve_fallback_constraints():
    import httpx

    from search_router.providers import ExaProvider, SerpAPIProvider
    from search_router.providers.keys import KeyPool

    settings = Settings(
        provider_order=["exa", "serpapi"],
        providers={
            "exa": {"keys": ["placeholder"]},
            "serpapi": {"keys": ["placeholder"], "engine": "bing"},
        },
    )
    request = SearchRequest(
        query="Tencent annual results", locale="en", include_domains=["hkexnews.hk"]
    )

    def upstream(outbound):
        assert outbound.url.params["q"] == "Tencent annual results (site:hkexnews.hk)"
        assert outbound.url.params["mkt"] == "en-US"
        return httpx.Response(200, json={"organic_results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        pool = KeyPool("exa", settings, [ExaProvider(settings.providers["exa"])])
        response = await Router(
            settings, [pool, SerpAPIProvider(settings.providers["serpapi"], client)]
        ).search(request, "scope")
    attempt = response.attempts[0]
    assert attempt.outcome == "skipped"
    assert attempt.reason == "unsupported_capability"
    assert attempt.usage.upstream_attempts == 0
    assert [issue.model_dump() for issue in attempt.diagnostics] == [
        {
            "field": "locale",
            "value": "en",
            "reason": "country_region_required",
        }
    ]
    assert attempt.key_attempts[0].diagnostics == attempt.diagnostics
    assert response.effective_request == request
    assert response.usage.upstream_attempts == 1


async def test_irrelevant_rows_do_not_leak_into_accepted_results():
    from search_router.contracts import SearchResult

    good = SearchResult(
        url="https://www.tencent.com/results",
        title="Tencent annual results",
        snippet="Tencent annual results and revenue",
        provider="tavily",
    )
    bad = SearchResult(
        url="https://example.org/africa",
        title="Most Profitable Companies in Africa",
        snippet="Annual results of leading companies",
        provider="tavily",
    )
    response = await Router(Settings(), [FakeProvider("tavily", [good, bad])]).search(
        SearchRequest(query="Tencent annual results"), "scope"
    )
    assert response.status == "ok"
    assert response.results == [good]
    assert "irrelevant_result_excluded" in response.warnings


@pytest.mark.parametrize(
    "updates",
    [
        {"query": "different detailed query"},
        {"limit": 1},
        {"category": "news"},
        {"locale": "en"},
        {"include_domains": ["example.org"]},
        {"exclude_domains": ["example.org"]},
        {"time_range": "week"},
        {"start_date": "2025-01-01"},
        {"end_date": "2025-12-31"},
        {"profile": "economy"},
        {"profile": "quality"},
    ],
)
async def test_every_request_field_partitions_cache(updates):
    provider = FakeProvider("exa")
    settings = Settings(profiles={"economy": ["exa"], "quality": ["exa"]})
    router = Router(settings, [provider])
    original = SearchRequest(query="market")
    changed = SearchRequest(query="market").model_dump(mode="json")
    changed.update(updates)
    changed = SearchRequest.model_validate(changed)
    assert (await router.search(original, "scope")).status == "empty"
    response = await router.search(changed, "scope")
    assert response.status == "empty"
    assert not response.cache.hit
    assert (await router.search(changed, "scope")).cache.hit
    assert (await router.search(original, "scope")).cache.hit
    assert provider.calls == 2
