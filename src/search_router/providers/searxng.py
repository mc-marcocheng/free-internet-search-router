from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from pydantic import field_validator

from ..contracts import ProviderCapabilities, SearchRequest
from .base import Batch, ProviderError
from .config import ProviderConfig, validate_endpoint
from .http import json_request, normalize, unsupported


class SearXNGConfig(ProviderConfig):
    http_url: str = "http://localhost:9090"

    @field_validator("http_url")
    @classmethod
    def base_url(cls, value: str) -> str:
        validate_endpoint(value)
        if urlsplit(value).query:
            raise ValueError("native SearXNG URL must be a base URL")
        return value


class SearXNGProvider:
    name = "searxng"

    def __init__(
        self, settings: SearXNGConfig, client: httpx.AsyncClient, api_key=None, timeout=10
    ):
        self.settings, self.client = settings, client
        self.inventory: dict | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            configured=bool(self.settings.http_url),
            categories=self.inventory["categories"] if self.inventory else ["general", "news"],
            locales=None,
            time_ranges=["day", "month", "year"],
            explicit_dates=False,
            engines=self.inventory["engines"] if self.inventory else None,
            diagnostics=[
                "native_http",
                "inventory_discovered" if self.inventory else "inventory_not_discovered",
                "language_and_time_support_engine_dependent",
            ],
        )

    async def discover(self) -> dict:
        data = await json_request(
            self.client,
            "GET",
            self.settings.http_url.rstrip("/") + "/config",
            headers={"Accept": "application/json"},
        )
        categories, engines = data.get("categories"), data.get("engines")
        if (
            not isinstance(categories, list)
            or not all(isinstance(category, str) for category in categories)
            or not isinstance(engines, list)
        ):
            raise ProviderError("malformed_response")
        self.inventory = {
            "categories": [category for category in ("general", "news") if category in categories],
            "engines": sorted(
                {
                    engine["name"]
                    for engine in engines
                    if isinstance(engine, dict)
                    and engine.get("enabled") is True
                    and isinstance(engine.get("name"), str)
                }
            ),
        }
        return {
            "transport": "http",
            **self.inventory,
            "locales": None,
            "time_ranges": ["day", "month", "year"],
            "explicit_dates": False,
            "search_health": "not_checked",
        }

    async def search(self, request: SearchRequest, charge: Callable[[], Awaitable[None]]) -> Batch:
        caps = self.capabilities()
        fields = [name for name in ("start_date", "end_date") if getattr(request, name)]
        if request.category not in caps.categories:
            fields.append("category")
        if request.time_range and request.time_range not in caps.time_ranges:
            fields.append("time_range")
        if fields:
            raise unsupported(request, *fields)
        params = {"q": request.query, "format": "json", "categories": request.category}
        if request.locale:
            params["language"] = request.locale
        if request.time_range:
            params["time_range"] = request.time_range
        await charge()
        data = await json_request(
            self.client,
            "POST",
            self.settings.http_url.rstrip("/") + "/search",
            data=params,
            headers={"Accept": "application/json"},
        )
        return normalize(data, self.name, engine_warnings=False)
