from typing import ClassVar, Literal

from pydantic import SecretStr

from ..contracts import ProviderCapabilities, QuotaObservation, SearchRequest
from .base import Batch, Charge, ProviderError
from .config import CredentialConfig
from .http import json_request, normalize, unsupported


class TavilyConfig(CredentialConfig):
    depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "basic"
    quota_unit: ClassVar[str] = "credits"


class TavilyProvider:
    name = "tavily"

    def __init__(
        self, settings: TavilyConfig, client, api_key: SecretStr | None = None, timeout: float = 10
    ):
        self.settings, self.client = settings, client
        keys = settings.credentials()
        self.api_key = api_key or (keys[0] if keys else None)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            configured=bool(self.api_key),
            categories=["general", "news"],
            time_ranges=["day", "week", "month", "year"],
            explicit_dates=True,
        )

    async def search(self, request: SearchRequest, charge: Charge) -> Batch:
        if not self.api_key:
            raise ProviderError("unconfigured")
        payload = {
            "query": request.query,
            "max_results": min(20, max(10, request.limit)),
            "topic": request.category,
            "search_depth": self.settings.depth,
            "include_domains": request.include_domains,
            "exclude_domains": request.exclude_domains,
            "include_answer": False,
            "include_raw_content": False,
            "auto_parameters": False,
            "include_usage": True,
            "include_published_date": True,
        }
        if request.locale:
            language = {"zh-TW": "zh-tw", "zh-Hant": "zh-tw", "zh-CN": "zh-cn"}.get(
                request.locale, request.locale.split("-")[0].lower()
            )
            if "-" in request.locale and request.locale not in {"zh-TW", "zh-Hant", "zh-CN"}:
                raise unsupported(request, "locale", reason="regional_locale_not_supported")
            payload.update(language=language, filter_by_language=True)
        for name in ("time_range", "start_date", "end_date"):
            value = getattr(request, name)
            if value:
                payload[name] = str(value)
        if request.time_range or request.start_date or request.end_date:
            payload["filter_by_published_date"] = True
        await charge()
        data = await json_request(
            self.client,
            "POST",
            "https://api.tavily.com/search",
            json=payload,
            headers={"Authorization": "Bearer " + self.api_key.get_secret_value()},
        )
        return normalize(data, self.name)

    async def quota_status(self) -> QuotaObservation:
        if not self.api_key:
            raise ProviderError("unconfigured")
        data = await json_request(
            self.client,
            "GET",
            "https://api.tavily.com/usage",
            headers={"Authorization": "Bearer " + self.api_key.get_secret_value()},
        )
        remaining = None
        account = data.get("account")
        if isinstance(account, dict):
            plan_limit, plan_usage = account.get("plan_limit"), account.get("plan_usage")
            if (
                type(plan_limit) is int
                and type(plan_usage) is int
                and min(plan_limit, plan_usage) >= 0
            ):
                remaining = max(0, plan_limit - plan_usage)
                paygo_limit, paygo_usage = account.get("paygo_limit"), account.get("paygo_usage")
                if (
                    type(paygo_limit) is int
                    and type(paygo_usage) is int
                    and min(paygo_limit, paygo_usage) >= 0
                ):
                    remaining += max(0, paygo_limit - paygo_usage)
        return QuotaObservation(remaining=remaining, unit="credits", scope="account")
