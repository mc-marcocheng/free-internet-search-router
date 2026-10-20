import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx
from pydantic import SecretStr, ValidationError

from .base import Provider
from .config import ProviderConfig


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    config_type: type[ProviderConfig]
    factory: Callable[..., Provider]
    default_enabled: bool = False


class ProviderRegistry:
    def __init__(self, specs: Iterable[ProviderSpec]):
        self.specs: dict[str, ProviderSpec] = {}
        for spec in specs:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", spec.name):
                raise ValueError("invalid provider registration name")
            if spec.name in self.specs:
                raise ValueError("duplicate provider registration")
            self.specs[spec.name] = spec

    def configurations(self, values: dict) -> dict[str, ProviderConfig]:
        if not isinstance(values, dict) or not values.keys() <= self.specs.keys():
            raise ValueError("unknown provider configuration")
        configs = {}
        for name, spec in self.specs.items():
            supplied = values.get(name, {})
            if isinstance(supplied, spec.config_type):
                configs[name] = supplied
                continue
            if not isinstance(supplied, dict):
                raise ValueError("invalid provider configuration")
            try:
                configs[name] = spec.config_type.model_validate(
                    {"enabled": spec.default_enabled, **supplied}
                )
            except ValidationError:
                raise ValueError("invalid provider configuration; check provider fields") from None
        return configs

    def adapter(
        self,
        name: str,
        config: ProviderConfig,
        client: httpx.AsyncClient,
        credential: SecretStr | None = None,
        timeout: float = 10,
    ) -> Provider:
        provider = self.specs[name].factory(config, client, api_key=credential, timeout=timeout)
        if provider.name != name:
            raise ValueError("provider name does not match registration")
        return provider

    def build(self, name: str, settings, client: httpx.AsyncClient) -> Provider:
        from .keys import KeyPool

        config = settings.providers[name]
        keys = config.credentials()
        if not keys:
            return self.adapter(name, config, client, timeout=settings.attempt_timeout)
        adapters = [
            self.adapter(name, config, client, key, settings.attempt_timeout) for key in keys
        ]
        return KeyPool(name, settings, adapters)


def builtin_registry() -> ProviderRegistry:
    from .exa import ExaConfig, ExaProvider
    from .searxng import SearXNGConfig, SearXNGProvider
    from .serpapi import SerpAPIConfig, SerpAPIProvider
    from .tavily import TavilyConfig, TavilyProvider

    return ProviderRegistry(
        [
            ProviderSpec("searxng", SearXNGConfig, SearXNGProvider, True),
            ProviderSpec("exa", ExaConfig, ExaProvider, True),
            ProviderSpec("serpapi", SerpAPIConfig, SerpAPIProvider, True),
            ProviderSpec("tavily", TavilyConfig, TavilyProvider, True),
        ]
    )
