import asyncio
import secrets
import time
from dataclasses import dataclass

from ..contracts import KeyAttempt, QuotaObservation, SearchRequest, Usage
from .base import Batch, Provider, ProviderError
from .config import CredentialConfig
from .errors import classify_exception


@dataclass
class KeyState:
    inflight: int = 0
    calls: int = 0
    blocked_until: float = 0
    last_reason: str | None = None
    exhausted: bool = False
    quota: QuotaObservation | None = None
    generation: int = 0
    retry_until: float = 0
    next_refresh_at: float = 0
    refresh_failures: int = 0
    refresh_status: str = "not_checked"
    last_test: dict | None = None


class KeyPool:
    def __init__(
        self,
        name: str,
        settings,
        providers: list[Provider],
        clock=time.monotonic,
        wall_clock=time.time,
    ):
        self.config = settings.providers[name]
        if not providers or not isinstance(self.config, CredentialConfig):
            raise ValueError("key pools require credential-backed adapters")
        self.name, self.settings, self.providers, self.clock = name, settings, providers, clock
        self.strategy = self.config.key_strategy
        self.states = [KeyState() for provider in providers]
        self.lock = asyncio.Lock()
        self.next_index = 0
        self.preferred = 0
        self.blocked_until = 0
        self.retry_until = 0
        self.wall_clock = wall_clock
        self.on_change = lambda: None
        self.on_refresh = lambda: None

    def capabilities(self):
        caps = self.providers[0].capabilities().model_copy(deep=True)
        caps.diagnostics.extend(
            ["key_strategy:" + self.strategy, "key_count:" + str(len(self.providers))]
        )
        return caps

    async def _acquire(
        self, excluded: set[int], advance: bool = True, key_index: int | None = None
    ) -> int:
        async with self.lock:
            if key_index is not None and not 0 <= key_index < len(self.states):
                raise ProviderError("unknown_key_index")
            if self.blocked_until > self.clock():
                raise ProviderError("cooldown", self.blocked_until - self.clock())
            if (
                self.config.quota_scope == "shared"
                and any(state.exhausted for state in self.states)
                and any(state.inflight for state in self.states)
            ):
                raise ProviderError("all_keys_unavailable")
            available = [
                index
                for index, state in enumerate(self.states)
                if index not in excluded
                and (key_index is None or index == key_index)
                and state.blocked_until <= self.clock()
                and not (state.exhausted and state.inflight)
            ]
            if not available:
                raise ProviderError("all_keys_unavailable")
            if self.strategy == "failover":
                index = min(
                    available, key=lambda entry: (entry - self.preferred) % len(self.states)
                )
            elif self.strategy == "random":
                index = secrets.choice(available)
            else:
                index = min(
                    available,
                    key=lambda entry: (
                        self.states[entry].inflight if self.strategy == "least_inflight" else 0,
                        (entry - self.next_index) % len(self.states),
                    ),
                )
                if advance:
                    self.next_index = (index + 1) % len(self.states)
            self.states[index].inflight += 1
            return index

    async def search(
        self, request: SearchRequest, charge, *, key_index: int | None = None
    ) -> Batch:
        attempts: list[KeyAttempt] = []
        excluded: set[int] = set()
        retry_keys = key_index is None and self.strategy in {"round_robin", "failover"}
        maximum = min(len(self.providers), self.settings.key_max_attempts) if retry_keys else 1
        for position in range(maximum):
            try:
                index = await self._acquire(
                    excluded, advance=key_index is None, key_index=key_index
                )
            except ProviderError as failure:
                failure.key_attempts = attempts
                raise
            excluded.add(index)
            state = self.states[index]
            generation = state.generation
            shared_generation = tuple(item.generation for item in self.states)
            started = self.clock()
            charged = False
            record = KeyAttempt(key_index=index + 1, outcome="failed", reason="internal_error")

            async def reserve():
                nonlocal charged
                if charged:
                    raise ProviderError("duplicate_attempt_blocked")
                await charge()
                charged = True
                state.calls += 1

            try:
                batch = await self.providers[index].search(request, reserve)
                record.outcome, record.reason = "completed", "response_received"
                record.usage = batch.usage.model_copy(deep=True)
                state.last_reason = "response_received"
                shared_recovery = (
                    self.config.quota_scope == "shared"
                    and any(item.exhausted for item in self.states)
                    and shared_generation == tuple(item.generation for item in self.states)
                )
                if state.generation == generation and (state.exhausted or shared_recovery):
                    state.exhausted = False
                    state.quota = None
                    state.generation += 1
                    if shared_recovery:
                        for item in self.states:
                            if item.exhausted:
                                item.exhausted = False
                                item.quota = None
                                item.blocked_until = item.retry_until
                                item.generation += 1
                    self.on_change()
                if self.strategy == "failover" and key_index is None:
                    self.preferred = index
            except asyncio.CancelledError:
                state.last_reason = "cancelled"
                raise
            except Exception as error:
                failure = classify_exception(error)
                record.reason = state.last_reason = failure.reason
                record.diagnostics = failure.diagnostics
                if not charged:
                    record.outcome = "skipped"
                if failure.reason == "authentication":
                    state.blocked_until = max(
                        state.blocked_until, self.clock() + self.settings.cooldown_seconds
                    )
                independent_quota = (
                    failure.reason == "quota_exhausted" and self.config.quota_scope == "independent"
                )
                if failure.reason == "quota_exhausted":
                    reset_at = state.quota.reset_at if state.quota else None
                    state.exhausted = True
                    state.generation += 1
                    state.quota = QuotaObservation(
                        remaining=0,
                        unit=self.config.quota_unit,
                        source="search_error",
                        reset_at=reset_at,
                    )
                    state.retry_until = max(
                        state.retry_until, self.clock() + (failure.retry_after or 0)
                    )
                    delay = self.settings.key_quota_cooldown_seconds
                    if reset_at and reset_at.timestamp() > self.wall_clock():
                        delay = reset_at.timestamp() - self.wall_clock()
                    state.blocked_until = max(state.retry_until, self.clock() + delay)
                if failure.reason == "rate_limited" or (
                    failure.reason == "quota_exhausted" and not independent_quota
                ):
                    self.blocked_until = max(
                        self.blocked_until,
                        self.clock()
                        + max(
                            self.settings.key_quota_cooldown_seconds
                            if failure.reason == "quota_exhausted"
                            else self.settings.cooldown_seconds,
                            failure.retry_after or 0,
                        ),
                    )
                    if failure.reason == "rate_limited":
                        self.retry_until = self.blocked_until
                self.on_change()
                can_failover = independent_quota or failure.reason in {
                    "authentication",
                    "transport",
                    "timeout",
                }
                if (
                    retry_keys
                    and can_failover
                    and position + 1 < maximum
                    and self.has_available_keys()
                ):
                    continue
                failure.key_attempts = attempts
                raise failure from None
            finally:
                record.latency_ms = (self.clock() - started) * 1000
                record.usage.upstream_attempts = int(charged)
                record.usage.estimated_cost_usd = (
                    self.config.estimated_cost_usd if charged else None
                )
                attempts.append(record)
                state.inflight -= 1
            batch.key_attempts = attempts
            batch.usage = Usage(
                upstream_attempts=sum(item.usage.upstream_attempts for item in attempts)
            )
            actual = [item.usage for item in attempts if item.usage.upstream_attempts]
            for name in ("credits", "estimated_cost_usd", "billed_cost_usd"):
                values = [getattr(usage, name) for usage in actual]
                setattr(
                    batch.usage,
                    name,
                    sum(values) if values and all(value is not None for value in values) else None,
                )
            return batch
        raise ProviderError("all_keys_unavailable")

    async def discover(self):
        index = await self._acquire(set(), advance=False)
        try:
            return await self.providers[index].discover()
        finally:
            self.states[index].inflight -= 1

    def observe_quota(self, index: int, quota: QuotaObservation):
        state = self.states[index]
        if quota.remaining is None and state.exhausted:
            return
        state.quota = quota
        if quota.remaining == 0:
            state.exhausted = True
            delay = self.settings.key_quota_cooldown_seconds
            if quota.reset_at and quota.reset_at.timestamp() > self.wall_clock():
                delay = quota.reset_at.timestamp() - self.wall_clock()
            state.blocked_until = max(state.retry_until, self.clock() + delay)
            if self.config.quota_scope == "shared":
                self.blocked_until = max(self.blocked_until, state.blocked_until)
        elif quota.remaining is not None and state.exhausted:
            state.exhausted = False
            if state.last_reason != "authentication":
                state.blocked_until = state.retry_until
            if self.config.quota_scope == "shared":
                self.blocked_until = max(
                    self.retry_until, *(item.retry_until for item in self.states)
                )
                for item in self.states:
                    if item.exhausted:
                        item.exhausted = False
                        item.blocked_until = item.retry_until
                        item.quota = None
        state.generation += 1
        self.on_change()

    def key_status(self):
        return {
            "strategy": self.strategy,
            "key_count": len(self.states),
            "keys": [
                {
                    "key_index": index + 1,
                    "inflight": state.inflight,
                    "upstream_attempts": state.calls,
                    "last_reason": state.last_reason,
                    "cooldown_remaining_seconds": max(
                        0, max(state.blocked_until, self.blocked_until) - self.clock()
                    ),
                    "available": self.blocked_until <= self.clock()
                    and state.blocked_until <= self.clock()
                    and not (state.exhausted and state.inflight),
                    "quota": state.quota.model_dump(mode="json") if state.quota else None,
                    "quota_stale": state.quota is None
                    or (
                        self.wall_clock() - state.quota.observed_at.timestamp()
                        >= self.settings.quota_cache_ttl_seconds
                    ),
                    "quota_discovery_supported": callable(
                        getattr(self.providers[index], "quota_status", None)
                    ),
                    "quota_refresh_status": state.refresh_status,
                }
                for index, state in enumerate(self.states)
            ],
        }

    def has_available_keys(self) -> bool:
        return self.blocked_until <= self.clock() and any(
            state.blocked_until <= self.clock() and not (state.exhausted and state.inflight)
            for state in self.states
        )
