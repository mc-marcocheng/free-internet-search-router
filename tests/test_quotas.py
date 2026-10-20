import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from test_service import FakeProvider

from search_router.config import Settings
from search_router.contracts import QuotaObservation, SearchRequest
from search_router.providers import ProviderError, SerpAPIProvider, json_request, retry_delay
from search_router.providers.keys import KeyPool
from search_router.providers.quotas import QuotaTracker
from search_router.providers.tavily import TavilyProvider


class QuotaProvider(FakeProvider):
    def __init__(self, name="serpapi", remaining=10):
        super().__init__(name)
        self.remaining = remaining
        self.probes = 0
        self.probed = asyncio.Event()
        self.release = None
        self.probe_error = None

    async def quota_status(self):
        self.probes += 1
        self.probed.set()
        if self.release:
            await self.release.wait()
        if self.probe_error:
            raise self.probe_error
        return QuotaObservation(unit="searches", remaining=self.remaining)


async def charge():
    return None


@pytest.mark.parametrize("cost,remaining", [(0, 10), (3.25, 6.75), (10, 0), (12, 0)])
async def test_exa_usage_is_month_to_date_against_assumed_dollar_budget(
    monkeypatch, cost, remaining
):
    from search_router.providers.exa import ExaConfig, ExaProvider

    now = datetime(2026, 12, 20, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("search_router.providers.exa.utcnow", lambda: now)
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.url.host == "admin-api.exa.ai"
        assert request.url.path == "/team-management/api-keys/key-fixture/usage"
        assert request.headers["x-api-key"] == "private-service-key"
        assert request.url.params["start_date"] == "2026-12-01T00:00:00+00:00"
        assert request.url.params["end_date"] == now.isoformat()
        return httpx.Response(
            200,
            json={
                "id": "key-fixture",
                "period": {"start": "2026-12-01T00:00:00Z", "end": now.isoformat()},
                "total_cost_usd": cost,
                "api_key_name": "do-not-retain",
            },
        )

    config = ExaConfig(
        keys=["private-search-key"],
        service_key="private-service-key",
        usage_key_ids=["key-fixture"],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        quota = await ExaProvider(config, client).quota_status()
    assert len(calls) == 1
    assert quota.remaining == remaining
    assert quota.budget_usd == 10
    assert quota.unit == "usd"
    assert quota.source == "configured_budget"
    assert quota.reset_at == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert "private" not in quota.model_dump_json()
    assert "do-not-retain" not in quota.model_dump_json()


@pytest.mark.parametrize("value", [True, -1, 1.5, "2"])
def test_invalid_remaining_is_not_a_balance(value):
    with pytest.raises(ValidationError):
        QuotaObservation(unit="searches", remaining=value)


@pytest.mark.parametrize("cost", [True, -1, "2.5", None, float("inf"), float("nan")])
async def test_exa_invalid_usage_never_becomes_free_budget(monkeypatch, cost):
    import json

    from search_router.providers.exa import ExaConfig, ExaProvider

    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    monkeypatch.setattr("search_router.providers.exa.utcnow", lambda: now)
    payload = {
        "id": "fixture",
        "period": {"start": "2026-10-01T00:00:00Z", "end": now.isoformat()},
        "total_cost_usd": cost,
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=json.dumps(payload).encode())
        )
    ) as client:
        provider = ExaProvider(
            ExaConfig(
                keys=["search-secret"], service_key="service-secret", usage_key_ids=["fixture"]
            ),
            client,
        )
        with pytest.raises(ProviderError, match="malformed_response"):
            await provider.quota_status()


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate_limited"),
        (500, "transport"),
        (302, "upstream_rejected"),
    ],
)
async def test_exa_usage_http_failures_are_classified_without_raw_errors(status, reason):
    from search_router.providers.exa import ExaConfig, ExaProvider

    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Retry-After": "60", "Location": "https://other.example"},
            text="private-service-secret",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = ExaProvider(
            ExaConfig(
                keys=["search-secret"], service_key="service-secret", usage_key_ids=["fixture"]
            ),
            client,
        )
        with pytest.raises(ProviderError, match=reason) as caught:
            await provider.quota_status()
    assert len(calls) == 1
    assert "private-service-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "another-key"),
        ("period", {"start": "2026-10-02T00:00:00Z", "end": "2026-10-20T00:00:00Z"}),
        ("period", {"start": "2026-10-01T00:00:00Z", "end": "2026-10-19T00:00:00Z"}),
    ],
)
async def test_exa_usage_must_match_selected_key_and_full_month_to_date(monkeypatch, field, value):
    from search_router.providers.exa import ExaConfig, ExaProvider

    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    monkeypatch.setattr("search_router.providers.exa.utcnow", lambda: now)
    payload = {
        "id": "fixture",
        "period": {"start": "2026-10-01T00:00:00Z", "end": now.isoformat()},
        "total_cost_usd": 0,
    }
    payload[field] = value
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ProviderError, match="malformed_response"):
            await ExaProvider(
                ExaConfig(
                    keys=["search-secret"], service_key="service-secret", usage_key_ids=["fixture"]
                ),
                client,
            ).quota_status()


async def test_unconfigured_exa_quota_does_not_consume_probes():
    from search_router.providers.exa import ExaProvider

    settings = Settings(quota_refresh_enabled=True, providers={"exa": {"keys": ["search-secret"]}})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected upstream request"))
    ) as client:
        provider = ExaProvider(settings.providers["exa"], client)
        pool = KeyPool("exa", settings, [provider])
        tracker = QuotaTracker(settings, [pool])
        await tracker.start()
        try:
            with pytest.raises(ProviderError, match="quota_service_key_required"):
                await tracker.refresh(pool, 0, manual=True)
            await asyncio.sleep(0)
            assert tracker.probe_attempts == 0
            settings.providers["exa"].service_key = SecretStr("service-secret")
            with pytest.raises(ProviderError, match="quota_key_id_required"):
                await tracker.refresh(pool, 0, manual=True)
            assert tracker.probe_attempts == 0
        finally:
            await tracker.close()


@pytest.mark.parametrize("setting", ["key_quota_cooldown_seconds", "quota_cache_ttl_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_quota_intervals_are_positive_and_finite(setting, value):
    with pytest.raises(ValidationError):
        Settings(**{setting: value})


@pytest.mark.parametrize("remaining,expected", [(5, 5), (0, 0), (True, None), (-1, None)])
async def test_serpapi_quota_is_allowlisted_and_date_precision_is_retained(remaining, expected):
    def upstream(request):
        assert request.url.path == "/account.json"
        return httpx.Response(
            200,
            json={
                "total_searches_left": remaining,
                "plan_renewal_date": "2026-11-18",
                "api_key": "never-retain",
                "account_email": "private@example.org",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = SerpAPIProvider(
            Settings(providers={"serpapi": {"keys": ["test-secret"]}}).providers["serpapi"], client
        )
        quota = await provider.quota_status()
    assert quota.remaining == expected
    assert quota.renewal_date.isoformat() == "2026-11-18"
    assert quota.reset_at is None
    assert "never-retain" not in quota.model_dump_json()
    assert "private@example.org" not in quota.model_dump_json()


@pytest.mark.parametrize(
    "limit,usage",
    [
        (100, 10),
        (10, 11),
        (None, 10),
        (True, 0),
        (10, -1),
    ],
)
async def test_tavily_ignores_key_limits_and_usage(limit, usage):
    def upstream(request):
        assert request.headers["authorization"] == "Bearer test-secret"
        assert request.url.path == "/usage"
        return httpx.Response(
            200,
            json={
                "key": {"limit": limit, "usage": usage},
                "account": {"plan_usage": 0, "plan_limit": 100, "paygo_limit": None},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        quota = await TavilyProvider(
            Settings(providers={"tavily": {"keys": ["test-secret"]}}).providers["tavily"], client
        ).quota_status()
    assert quota.remaining == 100
    assert quota.unit == "credits"
    assert quota.reset_at is None


async def test_tavily_unlimited_key_uses_remaining_account_plan_credits():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "key": {"limit": None, "usage": 884},
                    "account": {
                        "plan_limit": 1000,
                        "plan_usage": 884,
                        "paygo_usage": 0,
                        "paygo_limit": None,
                    },
                },
            )
        )
    ) as client:
        quota = await TavilyProvider(
            Settings(providers={"tavily": {"keys": ["test-secret"]}}).providers["tavily"], client
        ).quota_status()
    assert quota.remaining == 116
    assert quota.unit == "credits"
    assert quota.scope == "account"


@pytest.mark.parametrize(
    "key,account,expected",
    [
        ({"limit": None}, {"plan_limit": 1000, "plan_usage": 1000}, 0),
        ({"limit": None}, {"plan_limit": 1000, "plan_usage": 1001}, 0),
        ({"limit": 100, "usage": 10}, {"plan_limit": 1000, "plan_usage": 990}, 10),
        ({"limit": 100, "usage": 95}, {"plan_limit": 1000, "plan_usage": 990}, 10),
        (
            {"limit": None},
            {"plan_limit": 1000, "plan_usage": 1000, "paygo_limit": 100, "paygo_usage": 20},
            80,
        ),
        ({"limit": None}, {}, None),
        ({"limit": None}, {"plan_limit": True, "plan_usage": 0}, None),
        ({"limit": None}, {"plan_limit": 1000, "plan_usage": -1}, None),
        ({"limit": None}, {"plan_limit": "1000", "plan_usage": 0}, None),
        ({"limit": 100, "usage": 10}, {"plan_limit": None, "plan_usage": 0}, None),
        (None, {"plan_limit": 1000, "plan_usage": 884}, 116),
        ({}, {"plan_limit": 1000, "plan_usage": 884}, 116),
        ({}, None, None),
    ],
)
async def test_tavily_account_credit_balance(key, account, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"key": key, "account": account})
        )
    ) as client:
        quota = await TavilyProvider(
            Settings(providers={"tavily": {"keys": ["test-secret"]}}).providers["tavily"], client
        ).quota_status()
    assert quota.remaining == expected
    assert quota.unit == "credits"
    assert quota.scope == "account"


async def test_reclassified_quota_keeps_retry_after():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429, json={"error": "Search quota exhausted"}, headers={"Retry-After": "7200"}
            )
        )
    ) as client:
        with pytest.raises(ProviderError) as error:
            await json_request(client, "GET", "https://example.org")
    assert error.value.reason == "quota_exhausted"
    assert error.value.retry_after == 7200
    assert retry_delay("inf") is None


async def test_observation_reset_retry_and_unknown_recovery():
    now = [100.0]
    wall = [datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp()]
    pool = KeyPool(
        "serpapi",
        Settings(),
        [FakeProvider("serpapi")],
        clock=lambda: now[0],
        wall_clock=lambda: wall[0],
    )
    state = pool.states[0]
    state.retry_until = 500
    pool.observe_quota(
        0,
        QuotaObservation(
            unit="searches",
            remaining=0,
            reset_at=datetime.fromtimestamp(wall[0], timezone.utc) + timedelta(seconds=60),
        ),
    )
    assert state.blocked_until == 500
    pool.observe_quota(0, QuotaObservation(unit="searches", remaining=None))
    assert state.exhausted and state.quota.remaining == 0
    pool.observe_quota(0, QuotaObservation(unit="searches", remaining=10))
    assert not state.exhausted and state.blocked_until == 500
    assert not pool.has_available_keys()
    now[0] = 500
    assert pool.has_available_keys()


async def test_failed_probe_does_not_erase_or_extend_exhaustion():
    provider = QuotaProvider()
    provider.probe_error = ProviderError("transport")
    settings = Settings()
    pool = KeyPool("serpapi", settings, [provider])
    pool.observe_quota(0, QuotaObservation(unit="searches", remaining=0))
    deadline = pool.states[0].blocked_until
    tracker = QuotaTracker(settings, [pool])
    await tracker.refresh(pool, 0)
    assert pool.states[0].blocked_until == deadline
    assert pool.states[0].quota.remaining == 0
    assert pool.states[0].refresh_status == "transport"
    assert tracker.probe_attempts == 1


async def test_search_exhaustion_wins_over_older_inflight_probe():
    provider = QuotaProvider()
    provider.release = asyncio.Event()
    provider.error = ProviderError("quota_exhausted")
    pool = KeyPool("serpapi", Settings(), [provider])
    tracker = QuotaTracker(Settings(), [pool])
    task = asyncio.create_task(tracker.refresh(pool, 0))
    await provider.probed.wait()
    with pytest.raises(ProviderError):
        await pool.search(SearchRequest(query="market"), charge)
    provider.release.set()
    await task
    assert pool.states[0].exhausted
    assert pool.states[0].quota.remaining == 0


async def test_background_probe_is_cached_coalesced_and_separate_from_searches():
    settings = Settings(quota_refresh_enabled=True, quota_refresh_hourly_limit=1)
    provider = QuotaProvider()
    pool = KeyPool("serpapi", settings, [provider])
    tracker = QuotaTracker(settings, [pool])
    await tracker.start()
    try:
        await asyncio.wait_for(provider.probed.wait(), 1)
        for _ in range(10):
            assert pool.key_status()["keys"][0]["quota"]["remaining"] == 10
        await asyncio.gather(
            *(pool.search(SearchRequest(query="market"), charge) for _ in range(10))
        )
        assert provider.probes == tracker.probe_attempts == 1
        assert provider.calls == 10
        assert pool.states[0].quota.remaining == 10
        assert pool.states[0].calls == 10
    finally:
        await tracker.close()
    assert tracker.task.done()


async def test_probe_ceiling_stops_other_keys_and_disabled_discovery_is_silent():
    settings = Settings(quota_refresh_enabled=True, quota_refresh_hourly_limit=1)
    first, second = QuotaProvider(), QuotaProvider()
    pool = KeyPool("serpapi", settings, [first, second])
    tracker = QuotaTracker(settings, [pool])
    await tracker.start()
    await asyncio.wait_for(first.probed.wait(), 1)
    assert pool.key_status()["keys"][0]["quota"]["remaining"] == 10
    await tracker.close()
    assert first.probes == 1 and second.probes == 0
    unsupported = KeyPool("exa", settings, [FakeProvider("exa")])
    tracker = QuotaTracker(settings, [unsupported])
    await tracker.start()
    await tracker.close()
    assert tracker.probe_attempts == 0


async def test_cache_restart_reorder_expiry_and_no_credentials(tmp_path):
    wall = [1000.0]
    monotonic = [100.0]
    settings = Settings(
        quota_cache_enabled=True,
        quota_cache_dir=str(tmp_path),
        providers={"serpapi": {"keys": ["first-secret", "second-secret"]}},
    )
    pool = KeyPool(
        "serpapi",
        settings,
        [FakeProvider("serpapi"), FakeProvider("serpapi")],
        clock=lambda: monotonic[0],
        wall_clock=lambda: wall[0],
    )
    pool.observe_quota(0, QuotaObservation(unit="searches", remaining=0))
    tracker = QuotaTracker(settings, [pool], wall_clock=lambda: wall[0])
    tracker.changed()
    await tracker.flush()
    content = tracker.path.read_text()
    assert "first-secret" not in content and "second-secret" not in content
    settings = Settings(
        quota_cache_enabled=True,
        quota_cache_dir=str(tmp_path),
        providers={"serpapi": {"keys": ["second-secret", "first-secret"]}},
    )
    reordered = KeyPool(
        "serpapi", settings, pool.providers, clock=lambda: monotonic[0], wall_clock=lambda: wall[0]
    )
    restored = QuotaTracker(settings, [reordered], wall_clock=lambda: wall[0])
    await restored.load()
    assert not reordered.states[0].exhausted
    assert reordered.states[1].exhausted
    assert reordered.states[1].blocked_until == 3700
    wall[0] += 3601
    monotonic[0] += 3601
    await restored.load()
    assert reordered.has_available_keys()
    reordered.states[0].blocked_until = monotonic[0] + 10
    assert reordered.has_available_keys()


@pytest.mark.parametrize(
    "content", ["not json", '{"version":1}', '{"entries":{"x":{"exhausted":"yes"}}}']
)
async def test_corrupt_cache_falls_back_to_memory(tmp_path, content):
    settings = Settings(quota_cache_enabled=True, quota_cache_dir=str(tmp_path))
    tracker = QuotaTracker(settings, [])
    tracker.path.write_text(content)
    await tracker.load()
    assert tracker.storage_status == "unavailable"


async def test_unwritable_cache_does_not_break_selection(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    settings = Settings(
        quota_cache_enabled=True,
        quota_cache_dir=str(blocked),
        providers={"exa": {"keys": ["secret"]}},
    )
    pool = KeyPool("exa", settings, [FakeProvider("exa")])
    tracker = QuotaTracker(settings, [pool])
    tracker.changed()
    await tracker.flush()
    assert tracker.storage_status == "unavailable"
    await pool.search(SearchRequest(query="market"), charge)


def test_quota_identity_ignores_search_cost_and_enablement_changes():
    original = Settings(providers={"serpapi": {"keys": ["test-key"]}})
    changed = Settings(
        providers={
            "serpapi": {
                "keys": ["test-key"],
                "enabled": False,
                "engine": "bing",
                "estimated_cost_usd": 0.01,
            }
        }
    )
    original_pool = KeyPool("serpapi", original, [FakeProvider("serpapi")])
    changed_pool = KeyPool("serpapi", changed, [FakeProvider("serpapi")])
    first = QuotaTracker(original, [original_pool])
    second = QuotaTracker(changed, [changed_pool])
    assert list(first.identities.values()) == list(second.identities.values())


async def test_cached_account_interface_does_not_probe_inline_or_expose_keys():
    from search_router.api.app import create_app

    token = "test-only-router-token-at-least-24"
    settings = Settings(
        tokens={"test": token}, providers={"serpapi": {"keys": ["test-private-key"]}}
    )
    provider = QuotaProvider()
    pool = KeyPool("serpapi", settings, [provider])
    pool.observe_quota(0, QuotaObservation(unit="searches", remaining=5))
    app = create_app(settings, providers=[pool])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + token},
        ) as client:
            for _ in range(3):
                response = await client.get("/providers/serpapi/quotas")
                assert response.status_code == 200
                assert response.json()["keys"][0]["quota"]["remaining"] == 5
                assert "test-private-key" not in response.text
            status = (await client.get("/providers")).json()
            assert status["quota_cache"]["probe_attempts"] == 0
            assert (
                status["providers"]["serpapi"]["key_pool"]["keys"][0]["quota"]["unit"] == "searches"
            )
    assert provider.probes == 0


async def test_shared_positive_balance_preserves_pool_throttle():
    now = [100.0]
    settings = Settings(providers={"serpapi": {"quota_scope": "shared"}})
    pool = KeyPool(
        "serpapi",
        settings,
        [FakeProvider("serpapi"), FakeProvider("serpapi")],
        clock=lambda: now[0],
    )
    pool.observe_quota(0, QuotaObservation(unit="searches", remaining=0))
    pool.retry_until = 500
    pool.observe_quota(0, QuotaObservation(unit="searches", remaining=10))
    assert pool.blocked_until == 500
    assert not pool.has_available_keys()
    now[0] = 500
    await pool.search(SearchRequest(query="market"), charge)
    assert pool.states[0].quota.remaining == 10


async def test_cancelling_atomic_writer_waits_for_disk_completion(tmp_path, monkeypatch):
    import os
    import threading

    settings = Settings(quota_cache_enabled=True, quota_cache_dir=str(tmp_path))
    tracker = QuotaTracker(settings, [])
    entered, release = threading.Event(), threading.Event()
    replace = os.replace

    def held_replace(source, target):
        entered.set()
        assert release.wait(2)
        replace(source, target)

    monkeypatch.setattr("search_router.providers.quotas.os.replace", held_replace)
    tracker.changed()
    task = asyncio.create_task(tracker.flush())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tracker.path.exists()
    assert list(tmp_path.glob(".quota-*")) == []
