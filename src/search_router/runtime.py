from contextlib import AsyncExitStack

import httpx

from .config import Settings
from .providers.base import AsyncCloseable, Provider
from .providers.keys import KeyPool
from .providers.quotas import QuotaTracker
from .reader import Reader
from .search.router import Router


class Runtime:
    def __init__(self, settings: Settings, providers: list[Provider] | None = None, reader=None):
        self.settings = settings
        self.injected_providers = providers
        self.reader = reader if reader is not None else Reader(settings)
        self.resources = AsyncExitStack()
        self.instances: list[Provider] = []
        self.router: Router
        self.quotas: QuotaTracker
        self.started = False

    async def start(self, start_quotas=True):
        seen = set()

        def own(provider):
            if id(provider) in seen:
                return
            seen.add(id(provider))
            if isinstance(provider, KeyPool):
                for child in provider.providers:
                    own(child)
            elif isinstance(provider, AsyncCloseable):
                self.resources.push_async_callback(provider.aclose)

        try:
            http = httpx.AsyncClient(
                timeout=self.settings.attempt_timeout, trust_env=False, follow_redirects=False
            )
            self.resources.push_async_callback(http.aclose)
            if self.injected_providers is not None:
                self.instances = list(self.injected_providers)
                for provider in self.instances:
                    own(provider)
            else:
                for name in self.settings.providers:
                    provider = self.settings.registry.build(name, self.settings, http)
                    self.instances.append(provider)
                    own(provider)
            if len({provider.name for provider in self.instances}) != len(self.instances):
                raise ValueError("duplicate provider instance")
            if any(provider.name not in self.settings.providers for provider in self.instances):
                raise ValueError("unregistered provider instance")
            self.router = Router(self.settings, self.instances)
            self.quotas = QuotaTracker(
                self.settings,
                [provider for provider in self.instances if isinstance(provider, KeyPool)],
            )
            self.resources.push_async_callback(self.quotas.close)
            self.resources.push_async_callback(self.router.close)
            if isinstance(self.reader, AsyncCloseable):
                self.resources.push_async_callback(self.reader.aclose)
            if start_quotas:
                await self.quotas.start()
            self.started = True
        except BaseException:
            await self.close()
            raise

    async def close(self):
        self.started = False
        await self.resources.aclose()
