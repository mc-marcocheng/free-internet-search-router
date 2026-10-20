import pytest
from pydantic import ValidationError

from search_router.config import Settings


@pytest.mark.parametrize("selected", [1, 2])
@pytest.mark.parametrize("strategy", ["round_robin", "failover", "random", "least_inflight"])
async def test_explicit_key_search_uses_only_that_key_and_shared_budget(selected, strategy):
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers.base import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.search.router import Budget

    settings = Settings(
        providers={"tavily": {"keys": ["first", "second", "third"], "key_strategy": strategy}},
        hourly_attempt_limit=1,
    )
    children = [
        FakeProvider("tavily"),
        FakeProvider("tavily", error=ProviderError("authentication")),
        FakeProvider("tavily"),
    ]
    pool = KeyPool("tavily", settings, children)
    budget = Budget(settings)

    async def charge():
        await budget.reserve("tavily")

    if selected == 1:
        with pytest.raises(ProviderError, match="authentication"):
            await pool.search(SearchRequest(query="fixture"), charge, key_index=selected)
    else:
        await pool.search(SearchRequest(query="fixture"), charge, key_index=selected)
    assert budget.attempts == 1
    assert [child.calls for child in children] == [int(index == selected) for index in range(3)]
    assert pool.next_index == 0
    assert pool.preferred == 0


async def test_manual_quota_refresh_shares_atomic_probe_ceiling():
    import asyncio

    from test_service import FakeProvider

    from search_router.contracts import QuotaObservation
    from search_router.providers.base import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.providers.quotas import QuotaTracker

    class Probe(FakeProvider):
        async def quota_status(self):
            self.calls += 1
            await asyncio.sleep(0)
            return QuotaObservation(remaining=12, unit="credits")

    settings = Settings(
        providers={"tavily": {"keys": ["first", "second"]}}, quota_refresh_hourly_limit=1
    )
    children = [Probe("tavily"), Probe("tavily")]
    pool = KeyPool("tavily", settings, children)
    tracker = QuotaTracker(settings, [pool])
    results = await asyncio.gather(
        *(tracker.refresh(pool, index, manual=True) for index in range(2)), return_exceptions=True
    )
    assert sum(child.calls for child in children) == tracker.probe_attempts == 1
    assert sum(isinstance(result, ProviderError) for result in results) == 1


async def test_manual_quota_refresh_preserves_backoff_and_retry_after():
    from test_service import FakeProvider

    from search_router.providers.base import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.providers.quotas import QuotaTracker

    class Probe(FakeProvider):
        async def quota_status(self):
            self.calls += 1
            raise ProviderError("authentication")

    settings = Settings(providers={"tavily": {"keys": ["fixture"]}})
    child = Probe("tavily")
    pool = KeyPool("tavily", settings, [child])
    tracker = QuotaTracker(settings, [pool])
    assert await tracker.refresh(pool, 0, manual=True) == 1
    assert pool.states[0].refresh_status == "authentication"
    with pytest.raises(ProviderError, match="quota_refresh_backoff"):
        await tracker.refresh(pool, 0, manual=True)
    pool.states[0].next_refresh_at = 0
    pool.states[0].refresh_failures = 0
    pool.retry_until = pool.clock() + 60
    with pytest.raises(ProviderError, match="quota_refresh_backoff"):
        await tracker.refresh(pool, 0, manual=True)
    assert child.calls == tracker.probe_attempts == 1


def test_comma_keys_trim_deduplicate_and_redact_individually():
    settings = Settings(providers={"exa": {"keys": ["alpha-key", "beta-key", "alpha-key"]}})
    assert [key.get_secret_value() for key in settings.providers["exa"].credentials()] == [
        "alpha-key",
        "beta-key",
    ]
    assert "alpha-key" in settings.secrets()
    assert "beta-key" in settings.secrets()
    assert settings.providers["searxng"].credentials() == []
    assert "alpha-key" not in repr(settings)


@pytest.mark.parametrize("strategies", [{"searxng": "round_robin"}, {"exa": "unknown"}])
def test_invalid_key_strategy_is_rejected(strategies):
    with pytest.raises(ValidationError):
        Settings(
            providers={name: {"key_strategy": value} for name, value in strategies.items()},
        )


async def test_round_robin_is_atomic_for_concurrent_requests():
    import asyncio

    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers.keys import KeyPool

    children = [FakeProvider("exa") for index in range(3)]
    pool = KeyPool("exa", Settings(), children)

    async def charge():
        return None

    batches = await asyncio.gather(
        *(pool.search(SearchRequest(query="market"), charge) for index in range(9))
    )
    assert [batch.key_attempts[0].key_index for batch in batches] == [1, 2, 3] * 3
    assert [provider.calls for provider in children] == [3, 3, 3]
    assert all(state.inflight == 0 for state in pool.states)


async def test_failover_sticks_to_next_healthy_key_and_counts_each_call():
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool

    children = [FakeProvider("exa", error=ProviderError("authentication")), FakeProvider("exa")]
    pool = KeyPool("exa", Settings(providers={"exa": {"key_strategy": "failover"}}), children)
    charges = []

    async def charge():
        charges.append(1)

    batch = await pool.search(SearchRequest(query="market"), charge)
    assert [attempt.key_index for attempt in batch.key_attempts] == [1, 2]
    assert batch.usage.upstream_attempts == 2
    await pool.search(SearchRequest(query="another"), charge)
    assert [provider.calls for provider in children] == [1, 2]
    assert len(charges) == 3


@pytest.mark.parametrize("strategy", ["round_robin", "failover"])
@pytest.mark.parametrize("reason", ["rate_limited", "quota_exhausted", "malformed_response"])
async def test_key_retry_does_not_evade_limits_or_retry_bad_payloads(reason, strategy):
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool

    children = [FakeProvider("exa", error=ProviderError(reason)), FakeProvider("exa")]
    pool = KeyPool(
        "exa",
        Settings(providers={"exa": {"key_strategy": strategy, "quota_scope": "shared"}}),
        children,
    )

    async def charge():
        return None

    with pytest.raises(ProviderError) as failure:
        await pool.search(SearchRequest(query="market"), charge)
    assert failure.value.reason == reason
    assert children[1].calls == 0
    assert len(failure.value.key_attempts) == 1
    assert all(state.inflight == 0 for state in pool.states)


@pytest.mark.parametrize("strategy", ["round_robin", "failover"])
async def test_router_reserves_each_key_attempt_against_chain_budget(strategy):
    from test_policy import result
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.search.router import Router

    settings = Settings(providers={"exa": {"key_strategy": strategy, "estimated_cost_usd": 0.01}})
    pool = KeyPool(
        "exa",
        settings,
        [
            FakeProvider("exa", error=ProviderError("authentication")),
            FakeProvider("exa", [result()]),
        ],
    )
    fallback = FakeProvider("serpapi", [result()])
    router = Router(settings, [pool, fallback])
    response = await router.search(SearchRequest(query="market"), "test")
    assert response.status == "ok"
    assert response.usage.upstream_attempts == 2
    assert response.usage.estimated_cost_usd == 0.02
    assert [record.key_index for record in response.attempts[1].key_attempts] == [1, 2]
    assert router.budget.attempts == 2
    assert fallback.calls == 0

    settings = Settings(max_attempts_per_request=1, providers={"exa": {"key_strategy": strategy}})
    children = [
        FakeProvider("exa", error=ProviderError("authentication")),
        FakeProvider("exa", [result()]),
    ]
    response = await Router(settings, [KeyPool("exa", settings, children)]).search(
        SearchRequest(query="market"), "test"
    )
    assert response.usage.upstream_attempts == 1
    assert children[1].calls == 0


@pytest.mark.parametrize("strategy", ["round_robin", "failover"])
async def test_independent_quota_failover_preserves_accounting_and_cooldown(strategy):
    from test_policy import result
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.search.router import Router

    now = [100.0]
    settings = Settings(
        cache_ttl=0, providers={"exa": {"key_strategy": strategy, "estimated_cost_usd": 0.01}}
    )
    children = [
        FakeProvider("exa", error=ProviderError("quota_exhausted")),
        FakeProvider("exa", [result()]),
    ]
    pool = KeyPool("exa", settings, children, clock=lambda: now[0])
    router = Router(settings, [pool])
    response = await router.search(SearchRequest(query="market"), "test")
    assert response.status == "ok"
    assert response.usage.upstream_attempts == 2
    assert response.usage.estimated_cost_usd == 0.02
    assert pool.states[0].blocked_until == 3700
    assert router.health["exa"].blocked_until == 0
    now[0] = 3699
    await router.search(SearchRequest(query="another market"), "test")
    assert [child.calls for child in children] == [1, 2]
    assert router.budget.attempts == 3


async def test_quota_recovery_half_open_is_single_and_cancellation_releases():
    import asyncio

    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool

    now = [0.0]
    child = FakeProvider("exa", error=ProviderError("quota_exhausted"))
    pool = KeyPool("exa", Settings(), [child], clock=lambda: now[0])

    async def charge():
        return None

    with pytest.raises(ProviderError, match="quota_exhausted"):
        await pool.search(SearchRequest(query="market"), charge)
    now[0] = 3600
    child.error, child.wait = None, asyncio.Event()
    child.entered.clear()
    task = asyncio.create_task(pool.search(SearchRequest(query="market"), charge))
    await child.entered.wait()
    with pytest.raises(ProviderError, match="all_keys_unavailable"):
        await pool.search(SearchRequest(query="market"), charge)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.has_available_keys()
    child.wait.set()
    await pool.search(SearchRequest(query="market"), charge)
    assert not pool.states[0].exhausted


async def test_all_keys_blocked_is_zero_cost_skip_and_does_not_mask_recovery():
    from test_policy import result
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.search.router import Router

    now = [0.0]
    settings = Settings(cache_ttl=0)
    child = FakeProvider("exa", error=ProviderError("quota_exhausted"))
    pool = KeyPool("exa", settings, [child], clock=lambda: now[0])
    router = Router(settings, [pool, FakeProvider("serpapi", [result()])])
    await router.search(SearchRequest(query="market"), "test")
    response = await router.search(SearchRequest(query="market"), "test")
    skipped = next(attempt for attempt in response.attempts if attempt.provider == "exa")
    assert skipped.outcome == "skipped"
    assert skipped.usage.upstream_attempts == 0
    assert child.calls == 1
    now[0] = 3600
    child.error, child.results = None, [result()]
    response = await router.search(SearchRequest(query="market"), "test")
    assert response.selected_provider == "exa"


@pytest.mark.parametrize(
    "category,window,engine,filter_name,filter_value",
    [
        ("general", "day", "bing", "filters", 'ex1:"ez1"'),
        ("news", "week", "bing_news", "qft", 'interval="8"'),
    ],
)
async def test_bing_mapping(category, window, engine, filter_name, filter_value):
    import httpx

    from search_router.contracts import SearchRequest
    from search_router.providers import SerpAPIProvider

    def upstream(request):
        params = request.url.params
        assert params["engine"] == engine
        assert params["mkt"] == "zh-TW"
        assert params[filter_name] == filter_value
        assert "tbm" not in params and "tbs" not in params and "hl" not in params
        assert "site:example.org" in params["q"]
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {"title": "Market", "link": "https://example.org", "snippet": "Market results"}
                ]
            },
        )

    async def charge():
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = SerpAPIProvider(
            Settings(providers={"serpapi": {"keys": ["test-key"], "engine": "bing"}}).providers[
                "serpapi"
            ],
            client,
        )
        batch = await provider.search(
            SearchRequest(
                query="market",
                category=category,
                time_range=window,
                locale="zh-Hant",
                include_domains=["example.org"],
            ),
            charge,
        )
        assert batch.results[0].engines == ["bing"]


async def test_random_strategy_selects_one_key_without_retry(monkeypatch):
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers.keys import KeyPool

    monkeypatch.setattr("search_router.providers.keys.secrets.choice", lambda choices: choices[-1])
    children = [FakeProvider("exa"), FakeProvider("exa")]
    pool = KeyPool("exa", Settings(providers={"exa": {"key_strategy": "random"}}), children)

    async def charge():
        return None

    response = await pool.search(SearchRequest(query="market"), charge)
    assert response.key_attempts[0].key_index == 2
    assert [child.calls for child in children] == [0, 1]


async def test_least_inflight_and_cancellation_release_reservations():
    import asyncio

    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers.keys import KeyPool

    release = asyncio.Event()
    children = [FakeProvider("exa", wait=release), FakeProvider("exa", wait=release)]
    pool = KeyPool("exa", Settings(providers={"exa": {"key_strategy": "least_inflight"}}), children)

    async def charge():
        return None

    first = asyncio.create_task(pool.search(SearchRequest(query="market"), charge))
    await children[0].entered.wait()
    second = asyncio.create_task(pool.search(SearchRequest(query="market"), charge))
    await children[1].entered.wait()
    assert [state.inflight for state in pool.states] == [1, 1]
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert [state.inflight for state in pool.states] == [0, 1]
    children[0].entered.clear()
    third = asyncio.create_task(pool.search(SearchRequest(query="market"), charge))
    await children[0].entered.wait()
    release.set()
    await asyncio.gather(second, third)
    assert all(state.inflight == 0 for state in pool.states)


async def test_bad_round_robin_key_does_not_block_the_next_key():
    from test_policy import result
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.search.router import Router

    settings = Settings(cache_ttl=0)
    pool = KeyPool(
        "exa",
        settings,
        [
            FakeProvider("exa", error=ProviderError("authentication")),
            FakeProvider("exa", [result()]),
        ],
    )
    router = Router(settings, [pool])
    response = await router.search(SearchRequest(query="market"), "test")
    assert response.status == "ok"
    assert [attempt.key_index for attempt in response.attempts[1].key_attempts] == [1, 2]
    assert (await router.search(SearchRequest(query="market"), "test")).status == "ok"


@pytest.mark.parametrize("strategy", ["round_robin", "failover"])
@pytest.mark.parametrize(
    "budget,reason",
    [
        ({"hourly_attempt_limit": 1}, "attempt_budget_exhausted"),
        (
            {"hourly_cost_limit_usd": 0.01},
            "cost_budget_exhausted",
        ),
    ],
)
async def test_key_retry_budget_exhaustion_preserves_failed_key_usage(strategy, budget, reason):
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.search.router import Router

    settings = Settings(
        **budget, providers={"exa": {"key_strategy": strategy, "estimated_cost_usd": 0.01}}
    )
    children = [FakeProvider("exa", error=ProviderError("authentication")), FakeProvider("exa")]
    response = await Router(settings, [KeyPool("exa", settings, children)]).search(
        SearchRequest(query="market"), "test"
    )
    assert response.usage.upstream_attempts == 1
    assert [attempt.usage.upstream_attempts for attempt in response.attempts[1].key_attempts] == [
        1,
        0,
    ]
    assert response.attempts[1].reason == reason


@pytest.mark.parametrize("reason", ["authentication", "transport", "timeout"])
async def test_round_robin_retries_next_key_then_keeps_rotating(reason):
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool

    children = [
        FakeProvider("exa", error=ProviderError(reason)),
        FakeProvider("exa"),
        FakeProvider("exa"),
    ]
    pool = KeyPool("exa", Settings(), children)
    charges = []

    async def charge():
        charges.append(1)

    batch = await pool.search(SearchRequest(query="market"), charge)
    assert [attempt.key_index for attempt in batch.key_attempts] == [1, 2]
    assert [attempt.reason for attempt in batch.key_attempts] == [reason, "response_received"]
    assert batch.usage.upstream_attempts == 2
    following = await pool.search(SearchRequest(query="another"), charge)
    assert [attempt.key_index for attempt in following.key_attempts] == [3]
    assert [child.calls for child in children] == [1, 1, 1]
    assert len(charges) == 3
    assert all(state.inflight == 0 for state in pool.states)


@pytest.mark.parametrize("maximum,expected", [(1, 1), (2, 2), (8, 3)])
async def test_round_robin_bounds_retries_and_never_reuses_a_key(maximum, expected):
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool

    children = [FakeProvider("exa", error=ProviderError("transport")) for index in range(3)]
    pool = KeyPool("exa", Settings(key_max_attempts=maximum), children)
    charges = []

    async def charge():
        charges.append(1)

    with pytest.raises(ProviderError, match="transport") as failure:
        await pool.search(SearchRequest(query="market"), charge)
    assert [attempt.key_index for attempt in failure.value.key_attempts] == list(
        range(1, expected + 1)
    )
    assert [child.calls for child in children] == [1] * expected + [0] * (3 - expected)
    assert len(charges) == expected
    assert all(state.inflight == 0 for state in pool.states)


async def test_round_robin_exhausted_retries_fall_back_to_next_provider():
    from test_policy import result
    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.search.router import Router

    settings = Settings()
    children = [FakeProvider("exa", error=ProviderError("transport")) for index in range(2)]
    fallback = FakeProvider("serpapi", [result()])
    response = await Router(settings, [KeyPool("exa", settings, children), fallback]).search(
        SearchRequest(query="market"), "test"
    )
    assert response.status == "ok"
    assert response.selected_provider == "serpapi"
    assert response.usage.upstream_attempts == 3
    assert [attempt.key_index for attempt in response.attempts[1].key_attempts] == [1, 2]
    assert fallback.calls == 1


async def test_round_robin_retries_share_provider_deadline_and_release_keys():
    import asyncio

    from test_service import FakeProvider

    from search_router.contracts import SearchRequest
    from search_router.providers import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.search.router import Router

    settings = Settings(provider_order=["exa"], attempt_timeout=0.05, key_max_attempts=3)
    children = [
        FakeProvider("exa", error=ProviderError("transport")),
        FakeProvider("exa", wait=asyncio.Event()),
        FakeProvider("exa"),
    ]
    pool = KeyPool("exa", settings, children)
    response = await Router(settings, [pool]).search(SearchRequest(query="market"), "test")
    assert response.status == "unavailable"
    assert response.attempts[0].reason == "timeout"
    assert response.usage.upstream_attempts == 2
    assert children[1].cancelled.is_set()
    assert children[2].calls == 0
    assert all(state.inflight == 0 for state in pool.states)


def test_settings_do_not_read_credentials_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'ROUTER_TOKENS={"unexpected":"private-env-token"}', encoding="utf-8"
    )
    assert Settings().tokens == {}


async def test_exa_supplies_new_required_objective_only_when_advertised():
    from types import SimpleNamespace

    from mcp.types import CallToolResult, Tool

    from search_router.contracts import SearchRequest
    from search_router.providers import ExaProvider

    for requires_objective in (False, True):
        provider = ExaProvider(Settings(providers={"exa": {"keys": ["fake-key"]}}).providers["exa"])
        properties = {"query": {"type": "string"}, "numResults": {"type": "integer"}}
        required = ["query"]
        if requires_objective:
            properties["objective"] = {"type": "string", "minLength": 1}
            required.append("objective")
        provider.tools["web_search_exa"] = Tool(
            name="web_search_exa",
            inputSchema={"type": "object", "properties": properties, "required": required},
        )
        calls = []

        async def call_tool(name, arguments):
            assert ("objective" in arguments) == requires_objective
            return CallToolResult(content=[], structuredContent={"results": []})

        async def session(operation):
            return await operation(SimpleNamespace(call_tool=call_tool))

        async def charge():
            calls.append(1)

        provider._session = session
        await provider.search(SearchRequest(query="market"), charge)
        assert calls == [1]
