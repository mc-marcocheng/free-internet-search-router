import os

import httpx
import pytest

from search_router.config import BootstrapSettings
from search_router.contracts import SearchRequest
from search_router.search.router import Router
from search_router.storage import open_store


@pytest.mark.skipif(
    os.environ.get("ROUTER_LIVE_SMOKE") != "1", reason="live calls require explicit opt-in"
)
async def test_one_live_search_with_estimated_spending_ceiling(monkeypatch):
    name = os.environ.get("ROUTER_LIVE_PROVIDER")
    maximum = float(os.environ["ROUTER_LIVE_MAX_COST_USD"])
    for variable in ("ROUTER_LIVE_SMOKE", "ROUTER_LIVE_PROVIDER", "ROUTER_LIVE_MAX_COST_USD"):
        monkeypatch.delenv(variable, raising=False)
    assert 0 <= maximum <= 1, "live smoke estimate ceiling must be between zero and one USD"
    settings = open_store(BootstrapSettings()).load()[1]
    assert name in settings.providers, "choose exactly one registered live provider"
    estimate = settings.providers[name].estimated_cost_usd
    assert estimate is not None and estimate <= maximum, (
        "set a conservative per-call estimate first"
    )
    settings = settings.model_copy(
        update={
            "provider_order": [name],
            "hourly_attempt_limit": 1,
            "max_attempts_per_request": 1,
            "hourly_cost_limit_usd": maximum,
        }
    )
    async with httpx.AsyncClient(timeout=settings.attempt_timeout, trust_env=False) as client:
        provider = settings.registry.build(name, settings, client)
        assert provider.capabilities().configured, "configure credentials securely before opting in"
        router = Router(settings, [provider])
        try:
            response = await router.search(
                SearchRequest(query="Python programming language official documentation", limit=3),
                "live-smoke",
            )
            assert response.usage.upstream_attempts <= 1
            assert response.status == "ok", [attempt.reason for attempt in response.attempts]
        finally:
            await router.close()
