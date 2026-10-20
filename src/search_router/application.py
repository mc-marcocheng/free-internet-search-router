import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from functools import wraps

from pydantic import BaseModel

from .contracts import (
    Capabilities,
    ErrorResponse,
    ProviderDiscovery,
    ProviderList,
    ProviderQuotaStatus,
    ProviderStatus,
    QuotaCacheStatus,
    ReadRequest,
    ReadResponse,
    SearchRequest,
    SearchResponse,
)
from .providers.base import Discoverable, QuotaSource
from .providers.errors import classify_exception
from .providers.keys import KeyPool
from .runtime import Runtime


def runtime_operation(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        async with self.operation():
            return await method(self, *args, **kwargs)

    return guarded


class Gateway:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.condition = asyncio.Condition()
        self.active_operations = 0
        self.updating = False

    @asynccontextmanager
    async def operation(self):
        async with self.condition:
            await self.condition.wait_for(lambda: not self.updating)
            self.active_operations += 1
        try:
            yield
        finally:
            async with self.condition:
                self.active_operations -= 1
                self.condition.notify_all()

    async def reconfigure(self, settings):
        async with self.condition:
            await self.condition.wait_for(lambda: not self.updating)
            self.updating = True
            replacement = Runtime(settings)
            previous = self.runtime
            try:
                await self.condition.wait_for(lambda: self.active_operations == 0)
                await replacement.start(start_quotas=False)
                await previous.quotas.close()
                replacement.router.budget = previous.router.budget
                replacement.router.budget.settings = settings
                replacement.router.logical_requests = previous.router.logical_requests
                for name in replacement.router.health:
                    if name in previous.router.health:
                        replacement.router.health[name] = previous.router.health[name]
                old_pools = {pool.name: pool for pool in previous.quotas.pools}
                for pool in replacement.quotas.pools:
                    old = old_pools.get(pool.name)
                    if old is None or old.config.quota_identity() != pool.config.quota_identity():
                        continue
                    states = {
                        key.get_secret_value(): state
                        for key, state in zip(old.config.credentials(), old.states)
                    }
                    for index, key in enumerate(pool.config.credentials()):
                        if key.get_secret_value() in states:
                            pool.states[index] = deepcopy(states[key.get_secret_value()])
                    if old.config.quota_scope == pool.config.quota_scope:
                        pool.blocked_until, pool.retry_until = old.blocked_until, old.retry_until
                replacement.quotas.probes = previous.quotas.probes
                replacement.quotas.probe_attempts = previous.quotas.probe_attempts
                replacement.quotas.dirty = settings.quota_cache_enabled
                await replacement.quotas.flush()
                await replacement.quotas.start(load=False)
                self.runtime = replacement
                await previous.close()
            except BaseException:
                if self.runtime is previous:
                    await replacement.close()
                    if previous.quotas.task is None or previous.quotas.task.done():
                        await previous.quotas.start(load=False)
                raise
            finally:
                self.updating = False
                self.condition.notify_all()

    def safe(self, result: BaseModel):
        return type(result).model_validate(
            self.runtime.router.redact(result.model_dump(mode="json"))
        )

    @runtime_operation
    async def search(self, request: SearchRequest, scope: str) -> SearchResponse | ErrorResponse:
        if request.profile != "default" and request.profile not in self.runtime.settings.profiles:
            return ErrorResponse(error="unknown_profile")
        return await self.runtime.router.search(request, scope)

    @runtime_operation
    async def read(self, request: ReadRequest) -> ReadResponse:
        return self.safe(await self.runtime.reader.read(request))

    def capabilities(self) -> Capabilities:
        settings = self.runtime.settings
        return self.safe(
            Capabilities(
                providers=[provider.capabilities() for provider in self.runtime.instances],
                profiles={"default": settings.provider_order, **settings.profiles},
                reader_limit_bytes=settings.reader_limit_bytes,
                policy_version=settings.policy_version,
            )
        )

    def quota_cache(self) -> QuotaCacheStatus:
        return QuotaCacheStatus(
            storage_status=self.runtime.quotas.storage_status,
            probe_attempts=self.runtime.quotas.probe_attempts,
            hourly_probe_limit=self.runtime.settings.quota_refresh_hourly_limit,
        )

    def operations(self, provider) -> list[str]:
        adapters = provider.providers if isinstance(provider, KeyPool) else [provider]
        operations = []
        if all(isinstance(adapter, Discoverable) for adapter in adapters):
            operations.append("discover")
        if all(isinstance(adapter, QuotaSource) for adapter in adapters):
            operations.append("quotas")
        return operations

    def providers(self) -> ProviderList:
        status = self.runtime.router.status()
        status["providers"] = {
            name: ProviderStatus(
                name=name,
                enabled=self.runtime.settings.providers[name].enabled,
                capabilities=provider.capabilities(),
                operations=self.operations(provider),
                **status["providers"][name],
            )
            for name, provider in self.runtime.router.providers.items()
        }
        return self.safe(ProviderList(**status, quota_cache=self.quota_cache()))

    def provider(self, name: str) -> ProviderStatus | ErrorResponse:
        return self.providers().providers.get(name) or ErrorResponse(error="unknown_provider")

    @runtime_operation
    async def discover(self, name: str) -> ProviderDiscovery | ErrorResponse:
        provider = self.runtime.router.providers.get(name)
        if provider is None:
            return ErrorResponse(error="unknown_provider")
        if "discover" not in self.operations(provider):
            return ErrorResponse(error="unsupported_operation")
        if not provider.capabilities().configured:
            return ErrorResponse(error="unconfigured")
        try:
            async with asyncio.timeout(self.runtime.settings.attempt_timeout):
                details = await provider.discover()
                if len(json.dumps(details)) > 5_000_000:
                    return ErrorResponse(error="malformed_response")
                return self.safe(ProviderDiscovery(provider=name, details=details))
        except Exception as error:
            return ErrorResponse(error=classify_exception(error).reason)

    def quotas(self, name: str) -> ProviderQuotaStatus | ErrorResponse:
        provider = self.runtime.router.providers.get(name)
        if provider is None:
            return ErrorResponse(error="unknown_provider")
        if "quotas" not in self.operations(provider):
            return ErrorResponse(error="unsupported_operation")
        if not isinstance(provider, KeyPool) or not provider.capabilities().configured:
            return ErrorResponse(error="unconfigured")
        return self.safe(
            ProviderQuotaStatus(
                provider=name,
                configured=True,
                keys=provider.key_status()["keys"],
                quota_cache=self.quota_cache(),
            )
        )
