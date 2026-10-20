from collections.abc import Awaitable, Callable
from datetime import date, timedelta

import httpx
from pydantic import SecretStr

from ..config import Settings
from ..models import (
    CapabilityIssue,
    ProviderCapabilities,
    QuotaObservation,
    SearchRequest,
    utcnow,
)
from .base import Batch, ProviderError
from .common import json_request, normalize, unsupported


class SerpAPIProvider:
    name = "serpapi"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        api_key: SecretStr | None = None,
    ):
        self.settings, self.client = settings, client
        keys = settings.provider_keys(self.name)
        self.api_key = api_key or (keys[0] if keys else None)

    def capabilities(self) -> ProviderCapabilities:
        configured = bool(self.api_key)
        supported = self.settings.serpapi_engine in {"google", "bing"}
        return ProviderCapabilities(
            name=self.name,
            configured=configured,
            categories=["general", "news"] if supported else [],
            time_ranges=["day", "week", "month", "year"] if supported else [],
            explicit_dates=supported,
            diagnostics=(
                ["bing_news_no_explicit_dates_or_year", "relative_publication_dates_remain_unknown"]
                if self.settings.serpapi_engine == "bing"
                else []
            )
            if supported
            else ["unsupported_engine_mapping"],
        )

    async def search(self, request: SearchRequest, charge: Callable[[], Awaitable[None]]) -> Batch:
        caps = self.capabilities()
        if not caps.configured:
            raise ProviderError("unconfigured")
        if not caps.categories:
            raise ProviderError(
                "unsupported_capability",
                diagnostics=[
                    CapabilityIssue(
                        field="engine",
                        value=self.settings.serpapi_engine,
                        reason="unsupported_engine_mapping",
                    )
                ],
            )
        key = self.api_key.get_secret_value()
        bing = self.settings.serpapi_engine == "bing"
        if (
            not bing
            and request.category == "news"
            and (request.include_domains or request.exclude_domains)
        ):
            raise unsupported(
                request,
                *[
                    name
                    for name in ("include_domains", "exclude_domains")
                    if getattr(request, name)
                ],
                reason="google_news_domain_filter_not_supported",
            )
        query = request.query
        if request.include_domains:
            query += (
                " (" + " OR ".join("site:" + domain for domain in request.include_domains) + ")"
            )
        query += "".join(" -site:" + domain for domain in request.exclude_domains)
        params = {
            "api_key": key,
            "engine": self.settings.serpapi_engine,
            "q": query,
            "output": "json",
        }
        if bing:
            params.update(self.bing_parameters(request))
        elif request.category == "news":
            params["tbm"] = "nws"
        if request.locale and not bing:
            language = {"zh-TW": "zh-TW", "zh-Hant": "zh-TW", "zh-CN": "zh-CN"}.get(
                request.locale, request.locale.split("-")[0]
            )
            parts = request.locale.split("-")
            params.update(hl=language, lr="lang_" + language)
            if len(parts) == 2 and len(parts[1]) == 2:
                params["gl"] = parts[1].lower()
            elif len(parts) > 1 and request.locale != "zh-Hant":
                raise unsupported(request, "locale")
        if request.time_range and not bing:
            params["tbs"] = (
                "qdr:" + {"day": "d", "week": "w", "month": "m", "year": "y"}[request.time_range]
            )
        elif not bing and (request.start_date or request.end_date):
            params["tbs"] = "cdr:1"
            if request.start_date:
                params["tbs"] += ",cd_min:" + request.start_date.strftime("%m/%d/%Y")
            if request.end_date:
                params["tbs"] += ",cd_max:" + request.end_date.strftime("%m/%d/%Y")
        await charge()
        data = await json_request(
            self.client, "GET", "https://serpapi.com/search.json", serpapi=True, params=params
        )
        echoed = data.get("search_parameters", {})
        if isinstance(echoed, dict) and "q" in echoed and echoed["q"] != query:
            raise ProviderError("query_mismatch")
        result_key = (
            "news_results" if request.category == "news" and not bing else "organic_results"
        )
        info = data.get("search_information", {})
        if result_key not in data and isinstance(info, dict) and info.get("total_results") == 0:
            data[result_key] = []
        batch = normalize(data, self.name, result_key)
        for result in batch.results:
            result.engines = ["bing" if bing else "google"]
        return batch

    def bing_parameters(self, request: SearchRequest) -> dict:
        params = {"engine": "bing_news" if request.category == "news" else "bing"}
        if request.locale:
            aliases = {"en": "en-US", "zh-hant": "zh-TW"}
            markets = {
                value.lower(): value
                for value in (
                    "en-US",
                    "en-GB",
                    "en-AU",
                    "en-CA",
                    "zh-TW",
                    "zh-CN",
                    "zh-HK",
                    "fr-FR",
                    "de-DE",
                    "ja-JP",
                    "ko-KR",
                    "es-ES",
                    "it-IT",
                    "pt-BR",
                )
            }
            market = aliases.get(request.locale.lower()) or markets.get(request.locale.lower())
            if market is None:
                raise unsupported(request, "locale", reason="market_not_supported")
            params["mkt"] = market
        if request.category == "news":
            if request.start_date or request.end_date or request.time_range == "year":
                raise unsupported(
                    request,
                    *[
                        name
                        for name in ("start_date", "end_date", "time_range")
                        if getattr(request, name)
                    ],
                    reason="bing_news_constraint_not_supported",
                )
            if request.time_range:
                interval = {"day": 7, "week": 8, "month": 9}[request.time_range]
                params["qft"] = f'interval="{interval}"'
        elif request.time_range in {"day", "week", "month"}:
            period = {"day": 1, "week": 2, "month": 3}[request.time_range]
            params["filters"] = f'ex1:"ez{period}"'
        elif request.time_range == "year" or request.start_date or request.end_date:
            today = utcnow().date()
            epoch = date(1970, 1, 1)
            start = (
                today - timedelta(days=365) if request.time_range else request.start_date or epoch
            )
            end = request.end_date or today
            if start < epoch or end < epoch:
                raise unsupported(
                    request,
                    *[
                        name
                        for name in ("start_date", "end_date")
                        if getattr(request, name) and getattr(request, name) < epoch
                    ],
                    reason="dates_before_epoch_not_supported",
                )
            params["filters"] = f'ex1:"ez5_{(start - epoch).days}_{(end - epoch).days}"'
        return params

    async def quota_status(self) -> QuotaObservation:
        if not self.api_key:
            raise ProviderError("unconfigured")
        data = await json_request(
            self.client,
            "GET",
            "https://serpapi.com/account.json",
            params={"api_key": self.api_key.get_secret_value()},
        )
        remaining = data.get("total_searches_left")
        renewal = data.get("plan_renewal_date")
        try:
            renewal = date.fromisoformat(renewal) if isinstance(renewal, str) else None
        except ValueError:
            renewal = None
        return QuotaObservation(
            remaining=remaining if type(remaining) is int and remaining >= 0 else None,
            unit="searches",
            renewal_date=renewal,
            scope="account",
        )
