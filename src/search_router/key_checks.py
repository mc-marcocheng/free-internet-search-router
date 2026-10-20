import asyncio
from datetime import datetime
from typing import Literal

from pydantic import Field

from .contracts import Contract, QuotaObservation, SearchRequest, Usage, utcnow
from .providers.base import ProviderError, QuotaSource
from .providers.errors import classify_exception
from .providers.keys import KeyPool


class KeyAction(Contract):
    revision: int = Field(ge=1)
    provider: str
    index: int = Field(ge=1)
    action: Literal["test", "refresh_quota"]
    confirm_live: Literal[True]
    max_cost_usd: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class KeyActionResult(Contract):
    provider: str
    key_index: int
    action: Literal["test", "refresh_quota"]
    status: Literal["working", "refreshed", "failed", "skipped", "unsupported"] = "skipped"
    reason: str
    checked_at: datetime = Field(default_factory=utcnow)
    usage: Usage = Field(default_factory=Usage)
    quota_probe_attempts: int = 0
    quota: QuotaObservation | None = None


def key_status(runtime) -> dict:
    return runtime.router.redact(
        {
            pool.name: [
                {
                    **status,
                    "test": pool.states[index].last_test,
                    "quota_configuration_error": getattr(
                        pool.providers[index], "quota_configuration_error", None
                    ),
                }
                for index, status in enumerate(pool.key_status()["keys"])
            ]
            for pool in runtime.instances
            if isinstance(pool, KeyPool)
        }
    )


class KeyChecks:
    def __init__(self, runtime):
        self.runtime = runtime

    async def run(self, action: KeyAction) -> KeyActionResult:
        pool = self.runtime.router.providers.get(action.provider)
        report = KeyActionResult(
            provider=action.provider,
            key_index=action.index,
            action=action.action,
            reason="unknown_key_index",
        )
        index = action.index - 1
        if not isinstance(pool, KeyPool) or index >= len(pool.states):
            return report
        state = pool.states[index]
        if action.action == "refresh_quota":
            if not isinstance(pool.providers[index], QuotaSource):
                return report.model_copy(
                    update={"status": "unsupported", "reason": "quota_discovery_unsupported"}
                )
            try:
                report.quota_probe_attempts = await self.runtime.quotas.refresh(
                    pool, index, manual=True
                )
                report.status = "refreshed" if state.refresh_status == "completed" else "failed"
                report.reason = state.refresh_status
            except ProviderError as error:
                report.reason = error.reason
            report.quota = state.quota
            await self.runtime.quotas.flush()
            return KeyActionResult.model_validate(
                self.runtime.router.redact(report.model_dump(mode="json"))
            )
        estimate = pool.config.estimated_cost_usd
        if action.max_cost_usd is None:
            report.reason = "test_cost_ceiling_required"
        elif estimate is None:
            report.reason = "unknown_cost_estimate"
        elif estimate > action.max_cost_usd:
            report.reason = "test_cost_ceiling_exceeded"
        elif self.runtime.router.health[pool.name].blocked_until > self.runtime.router.clock():
            report.reason = "circuit_open"
        elif not pool.providers[index].capabilities().configured:
            report.reason = "unconfigured"
        else:
            charged = False
            reservation_started = False

            async def charge():
                nonlocal charged, reservation_started
                if reservation_started:
                    raise ProviderError("duplicate_attempt_blocked")
                reservation_started = True
                await self.runtime.router.budget.reserve(pool.name)
                charged = True

            try:
                async with asyncio.timeout(
                    min(self.runtime.settings.attempt_timeout, self.runtime.settings.total_timeout)
                ):
                    batch = await pool.search(
                        SearchRequest(query="Python official documentation", limit=1),
                        charge,
                        key_index=index,
                    )
                report.status = "working" if charged else "skipped"
                report.reason = "response_received" if charged else "no_upstream_call"
                report.usage = batch.usage
            except Exception as error:
                report.status = "failed" if charged else "skipped"
                report.reason = classify_exception(error).reason
            report.usage.upstream_attempts = int(charged)
            report.usage.estimated_cost_usd = estimate if charged else None
        report.quota = state.quota
        report = KeyActionResult.model_validate(
            self.runtime.router.redact(report.model_dump(mode="json"))
        )
        state.last_test = report.model_dump(mode="json")
        await self.runtime.quotas.flush()
        return report
