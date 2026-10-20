import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import quote, quote_plus
from uuid import uuid4

from .config import Settings
from .models import Attempt, CacheMetadata, SearchRequest, SearchResponse, Usage
from .policy import evaluate
from .providers import Provider, ProviderError, classify_exception
from .telemetry import Telemetry

INFRA_FAILURES = {"transport", "timeout", "malformed_response", "upstream_error", "internal_error"}
ACCOUNT_FAILURES = {"authentication", "quota_exhausted", "rate_limited", "all_keys_unavailable"}


@dataclass
class Health:
    failures: int = 0
    blocked_until: float = 0
    last_reason: str | None = None
    account_health: str = "unknown"
    search_health: str = "unknown"


class Budget:
    def __init__(self, settings: Settings, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.lock = asyncio.Lock()
        self.started = clock()
        self.attempts = 0
        self.by_provider: dict[str, int] = {}
        self.reserved_cost = 0.0

    async def reserve(self, provider: str):
        async with self.lock:
            if self.clock() - self.started >= 3600:
                self.started = self.clock()
                self.attempts, self.by_provider, self.reserved_cost = 0, {}, 0.0
            config = self.settings.providers[provider]
            estimate = config.estimated_cost_usd
            if self.attempts >= self.settings.hourly_attempt_limit:
                raise ProviderError("attempt_budget_exhausted")
            if (
                config.hourly_attempt_limit is not None
                and self.by_provider.get(provider, 0) >= config.hourly_attempt_limit
            ):
                raise ProviderError("provider_budget_exhausted")
            limit = self.settings.hourly_cost_limit_usd
            if limit is not None and (estimate is None or self.reserved_cost + estimate > limit):
                raise ProviderError(
                    "cost_budget_exhausted" if estimate is not None else "unknown_cost_budget"
                )
            self.attempts += 1
            self.by_provider[provider] = self.by_provider.get(provider, 0) + 1
            self.reserved_cost += estimate or 0


class Router:
    def __init__(self, settings: Settings, providers: list[Provider], clock=time.monotonic):
        self.settings = settings
        self.providers = {provider.name: provider for provider in providers}
        self.clock = clock
        self.health = {name: Health() for name in self.providers}
        self.budget = Budget(settings, clock)
        self.cache: OrderedDict[str, tuple[float, SearchResponse]] = OrderedDict()
        self.inflight: dict[str, asyncio.Task] = {}
        self.waiters: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self.telemetry = Telemetry()
        self.logical_requests = 0
        self.active_requests = 0
        self.policy_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "version": settings.policy_version,
                    "policy": settings.policy.model_dump(),
                    "order": settings.provider_order,
                    "profiles": settings.profiles,
                    "providers": {
                        name: config.cache_identity() for name, config in settings.providers.items()
                    },
                    "key_max_attempts": settings.key_max_attempts,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.settings.secrets():
                for variant in {secret, quote(secret, safe=""), quote_plus(secret)}:
                    value = value.replace(variant, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        return value

    async def search(self, request: SearchRequest, scope: str) -> SearchResponse:
        if request.profile != "default" and request.profile not in self.settings.profiles:
            raise ProviderError("unknown_profile")
        with self.telemetry.tracer.start_as_current_span(
            "router.search", record_exception=False, set_status_on_exception=False
        ) as span:
            response = await self._search(request, scope)
            span.set_attribute("status", response.status)
            span.set_attribute("cache_hit", response.cache.hit)
            return response

    async def _search(self, request: SearchRequest, scope: str) -> SearchResponse:
        self.logical_requests += 1
        self.telemetry.requests.add(1)
        request_id = str(uuid4())
        key = hashlib.sha256(
            json.dumps(
                {
                    "request": request.model_dump(mode="json"),
                    "scope": scope,
                    "policy": self.policy_fingerprint,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        async with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock() - cached[0] < self.settings.cache_ttl:
                self.cache.move_to_end(key)
                response = cached[1].model_copy(deep=True)
                response.request_id = request_id
                response.usage = Usage(upstream_attempts=0)
                response.cache = CacheMetadata(
                    hit=True,
                    age_seconds=self.clock() - cached[0],
                    ttl_seconds=self.settings.cache_ttl,
                    origin_request_id=cached[1].request_id,
                )
                self.telemetry.cache.add(1)
                return response
            self.cache.pop(key, None)
            if self.active_requests >= self.settings.max_concurrent_searches:
                return SearchResponse(
                    request_id=request_id,
                    effective_request=request,
                    status="unavailable",
                    warnings=["router_busy"],
                )
            self.active_requests += 1
            shared = key in self.inflight
            if not shared:
                self.inflight[key] = asyncio.create_task(self._run_and_cache(key, request))
                self.waiters[key] = 0
            task = self.inflight[key]
            self.waiters[key] += 1
        try:
            response = (await asyncio.shield(task)).model_copy(deep=True)
            response.request_id = request_id
            response.cache.coalesced = shared
            if shared:
                response.usage = Usage(upstream_attempts=0)
            return response
        finally:
            async with self.lock:
                self.active_requests -= 1
                self.waiters[key] -= 1
                if self.waiters[key] == 0:
                    if not task.done():
                        task.cancel()
                    self.inflight.pop(key, None)
                    self.waiters.pop(key, None)
            if task.cancelling():
                await asyncio.gather(task, return_exceptions=True)

    async def _run_and_cache(self, key: str, request: SearchRequest) -> SearchResponse:
        response = await self._route(request)
        response = SearchResponse.model_validate(self.redact(response.model_dump(mode="json")))
        response.cache.origin_request_id = response.request_id
        response.cache.ttl_seconds = self.settings.cache_ttl
        if response.status in {"ok", "empty"}:
            async with self.lock:
                self.cache[key] = (self.clock(), response.model_copy(deep=True))
                while len(self.cache) > self.settings.cache_entries:
                    self.cache.popitem(last=False)
        return response

    async def _route(self, request: SearchRequest) -> SearchResponse:
        response = SearchResponse(
            effective_request=request,
            status="unavailable",
            policy_version=self.settings.policy_version,
        )
        chain = (
            self.settings.provider_order
            if request.profile == "default"
            else self.settings.profiles.get(request.profile, [])
        )
        deadline = self.clock() + self.settings.total_timeout
        best = None
        best_rank = (-1.0, -1, -1)
        completed_empty = False
        issued = 0
        for name in chain:
            provider = self.providers.get(name)
            health = self.health.get(name)
            skip = None
            if not self.settings.providers[name].enabled or not provider:
                skip = "disabled"
            elif not provider.capabilities().configured:
                skip = "unconfigured"
            elif health.blocked_until > self.clock():
                skip = "cooldown"
            elif issued >= self.settings.max_attempts_per_request:
                skip = "request_attempt_limit"
            elif deadline <= self.clock():
                skip = "total_deadline"
            if skip:
                response.attempts.append(Attempt(provider=name, outcome="skipped", reason=skip))
                continue
            started = self.clock()
            charged = 0

            async def charge():
                nonlocal charged, issued
                maximum = (
                    min(self.settings.key_max_attempts, len(provider.providers))
                    if hasattr(provider, "key_status")
                    else 1
                )
                if charged >= maximum:
                    raise ProviderError("duplicate_attempt_blocked")
                if issued >= self.settings.max_attempts_per_request:
                    raise ProviderError("request_attempt_limit")
                await self.budget.reserve(name)
                charged += 1
                issued += 1
                self.telemetry.attempts.add(1, {"provider": name})

            attempt = Attempt(provider=name, outcome="failed", reason="internal_error")
            with self.telemetry.tracer.start_as_current_span(
                "provider.search",
                record_exception=False,
                set_status_on_exception=False,
                attributes={"provider": name},
            ) as span:
                try:
                    async with asyncio.timeout(
                        min(self.settings.attempt_timeout, deadline - self.clock())
                    ):
                        batch = await provider.search(request, charge)
                    decision = evaluate(
                        name, batch.results, request, self.settings.policy, batch.upstream_count
                    )
                    attempt.reason = decision.reason
                    attempt.valid_results = len(decision.results)
                    attempt.contributing_engines = decision.engines
                    attempt.usage = batch.usage
                    attempt.key_attempts = batch.key_attempts
                    response.warnings.extend(batch.warnings + decision.warnings)
                    health.failures = 0
                    health.account_health = "authenticated"
                    health.search_health = decision.reason
                    if decision.accepted:
                        attempt.outcome = "accepted"
                        response.status = "ok"
                        response.results = decision.results[: request.limit]
                        response.selected_provider = name
                    elif decision.results:
                        attempt.outcome = "partial"
                        rank = (decision.relevance, len(decision.results), decision.engines)
                        if rank > best_rank and (
                            decision.relevance > 0 or self.settings.policy.min_relevance == 0
                        ):
                            best_rank = rank
                            best = (name, decision.results[: request.limit])
                    else:
                        attempt.outcome = "empty" if decision.reason == "empty" else "failed"
                        completed_empty = completed_empty or decision.reason == "empty"
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    failure = classify_exception(error)
                    attempt.key_attempts = failure.key_attempts
                    attempt.reason = failure.reason
                    attempt.diagnostics = failure.diagnostics
                    attempt.outcome = (
                        "failed"
                        if charged
                        or failure.reason
                        not in {
                            "unconfigured",
                            "unsupported_capability",
                            "attempt_budget_exhausted",
                            "provider_budget_exhausted",
                            "cost_budget_exhausted",
                            "unknown_cost_budget",
                            "request_attempt_limit",
                            "cooldown",
                            "all_keys_unavailable",
                        }
                        else "skipped"
                    )
                    if failure.reason in INFRA_FAILURES:
                        health.failures += 1
                        if health.failures >= self.settings.circuit_threshold:
                            health.blocked_until = self.clock() + self.settings.cooldown_seconds
                    elif failure.reason in ACCOUNT_FAILURES:
                        health.account_health = failure.reason
                        remaining_key = hasattr(provider, "has_available_keys")
                        if not remaining_key:
                            health.blocked_until = self.clock() + max(
                                self.settings.cooldown_seconds, failure.retry_after or 0
                            )
                    health.search_health = "failed" if charged else health.search_health
                attempt.latency_ms = (self.clock() - started) * 1000
                attempt.usage.upstream_attempts = int(charged)
                attempt.usage.estimated_cost_usd = (
                    self.settings.providers[name].estimated_cost_usd * charged
                    if charged and self.settings.providers[name].estimated_cost_usd is not None
                    else None
                )
                health.last_reason = attempt.reason
                span.set_attribute("outcome", attempt.outcome)
                span.set_attribute("reason", attempt.reason)
            self.telemetry.decisions.add(
                1, {"provider": name, "outcome": attempt.outcome, "reason": attempt.reason}
            )
            self.telemetry.latency.record(attempt.latency_ms, {"provider": name})
            self.telemetry.quorum.record(attempt.contributing_engines, {"provider": name})
            if attempt.usage.credits is not None:
                self.telemetry.credits.add(attempt.usage.credits, {"provider": name})
            if attempt.usage.estimated_cost_usd is not None:
                self.telemetry.estimated_cost.add(
                    attempt.usage.estimated_cost_usd, {"provider": name}
                )
            response.attempts.append(attempt)
            if response.status == "ok":
                break
        if response.status != "ok":
            if best:
                response.status, response.selected_provider, response.results = (
                    "partial",
                    best[0],
                    best[1],
                )
                response.warnings.append("no_provider_met_acceptance_policy")
            elif completed_empty:
                response.status = "empty"
            else:
                response.status = "unavailable"
        response.warnings = sorted(set(response.warnings))
        actual = [attempt.usage for attempt in response.attempts if attempt.usage.upstream_attempts]
        response.usage.upstream_attempts = sum(usage.upstream_attempts for usage in actual)
        for field in ("estimated_cost_usd", "billed_cost_usd", "credits"):
            values = [getattr(usage, field) for usage in actual]
            setattr(
                response.usage,
                field,
                sum(values) if values and all(value is not None for value in values) else None,
            )
        return response

    async def close(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def status(self) -> dict:
        return {
            "logical_requests": self.logical_requests,
            "actual_upstream_attempts": self.budget.attempts,
            "budget_window_seconds": 3600,
            "reserved_estimated_cost_usd": self.budget.reserved_cost,
            "providers": {
                name: {
                    "account_health": health.account_health,
                    "search_health": health.search_health,
                    "last_reason": health.last_reason,
                    "consecutive_infrastructure_failures": health.failures,
                    "cooldown_remaining_seconds": max(0, health.blocked_until - self.clock()),
                    "key_pool": self.providers[name].key_status()
                    if hasattr(self.providers[name], "key_status")
                    else None,
                }
                for name, health in self.health.items()
            },
        }
