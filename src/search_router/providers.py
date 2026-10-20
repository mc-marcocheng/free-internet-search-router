import json
import math
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
import jsonschema
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from .config import Settings
from .models import (
    CapabilityIssue,
    ProviderCapabilities,
    QuotaObservation,
    SearchRequest,
    SearchResult,
    Usage,
    utcnow,
)
from .provider_contract import Batch, ProviderError


def unsupported(request: SearchRequest, *fields: str, reason="unsupported_value") -> ProviderError:
    values = request.model_dump(mode="json")
    return ProviderError(
        "unsupported_capability",
        diagnostics=[
            CapabilityIssue(
                field=name,
                value=(
                    value
                    if (value := values[name]) is None or isinstance(value, (str, list))
                    else str(value)
                ),
                reason=reason,
            )
            for name in fields
        ],
    )


def retry_delay(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
        return max(0, delay) if math.isfinite(delay) else None
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - utcnow()).total_seconds())
        except (ValueError, TypeError):
            return None


def check_http(response: httpx.Response) -> None:
    status = response.status_code
    if status in {401, 403}:
        raise ProviderError("authentication")
    if status in {402, 432, 433}:
        raise ProviderError("quota_exhausted", retry_delay(response.headers.get("retry-after")))
    if status == 429:
        raise ProviderError("rate_limited", retry_delay(response.headers.get("retry-after")))
    if status >= 500:
        raise ProviderError("transport")
    if status >= 300:
        raise ProviderError("upstream_rejected")


def error_object(payload: dict) -> None:
    error = payload.get("error") or payload.get("errors")
    if not error:
        return
    text = str(error).casefold()
    if "rate limit" in text or "429" in text:
        raise ProviderError("rate_limited")
    if any(word in text for word in ("quota", "credit", "limit exceeded", "run out")):
        raise ProviderError("quota_exhausted")
    if any(word in text for word in ("api key", "unauthorized", "forbidden", "authentication")):
        raise ProviderError("authentication")
    if "rate" in text or "429" in text:
        raise ProviderError("rate_limited")
    raise ProviderError("upstream_error")


def parse_date(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return parsed if parsed.tzinfo else None
        except (ValueError, TypeError):
            return None


def normalize(payload: dict, provider: str, key="results") -> Batch:
    error_object(payload)
    rows = payload.get(key)
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ProviderError("malformed_response")
    results = []
    warnings = []
    for row in rows:
        if not isinstance(row, dict):
            warnings.append("invalid_result")
            continue
        engines = row.get("engines", [row["engine"]] if row.get("engine") else [])
        if not isinstance(engines, list):
            engines = []
        snippet = row.get("content") or row.get("snippet") or row.get("text")
        if not snippet and isinstance(row.get("highlights"), list):
            snippet = "\n".join(value for value in row["highlights"] if isinstance(value, str))
        url, title = row.get("url") or row.get("link"), row.get("title")
        if not all(isinstance(value, str) for value in (url, title, snippet)):
            warnings.append("invalid_result")
            continue
        if any(len(value) > 100000 for value in (url, title, snippet)):
            warnings.append("oversize_result")
            continue
        results.append(
            SearchResult(
                url=url,
                title=title,
                snippet=snippet,
                provider=provider,
                engines=[engine for engine in engines if isinstance(engine, str)],
                published_at=parse_date(
                    row.get("publishedDate")
                    or row.get("published_date")
                    or row.get("published_at")
                    or row.get("date")
                ),
            )
        )
    if provider != "searxng" and payload.get("unresponsive_engines"):
        warnings.append("some_engines_unresponsive")
    credits = payload.get("usage", {})
    credits = credits.get("credits") if isinstance(credits, dict) else None
    if not isinstance(credits, (float, int)) or isinstance(credits, bool) or credits < 0:
        credits = None
    return Batch(
        results=results, upstream_count=len(rows), warnings=warnings, usage=Usage(credits=credits)
    )


async def json_request(
    client: httpx.AsyncClient, method: str, url: str, *, serpapi=False, **kwargs
) -> dict:
    async with client.stream(method, url, **kwargs) as response:
        rate_error = None
        try:
            check_http(response)
        except ProviderError as failure:
            if response.status_code != 429:
                raise
            rate_error = failure
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > 5000000:
                raise ProviderError("malformed_response")
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeError):
            raise (rate_error or ProviderError("malformed_response")) from None
        if not isinstance(payload, dict):
            raise rate_error or ProviderError("malformed_response")
        if rate_error:
            try:
                error_object(payload)
            except ProviderError as failure:
                if failure.reason == "quota_exhausted":
                    failure.retry_after = rate_error.retry_after
                    raise
            raise rate_error
        if serpapi:
            metadata = payload.get("search_metadata", {})
            info = payload.get("search_information", {})
            if not isinstance(metadata, dict) or not isinstance(info, dict):
                raise ProviderError("malformed_response")
            if metadata.get("status") in {"Error", "Processing", "Queued"}:
                raise ProviderError("upstream_error")
            fully_empty = any(
                str(value).casefold() == "fully empty"
                for key, value in info.items()
                if key.endswith("_state")
            )
            if (
                metadata.get("status") == "Success"
                and fully_empty
                and payload.get("error")
                in {
                    "Google hasn't returned any results for this query.",
                    "Bing hasn't returned any results for this query.",
                }
            ):
                payload.pop("error")
                payload.setdefault("organic_results", [])
                payload.setdefault("news_results", [])
        error_object(payload)
        return payload


class SearXNGProvider:
    name = "searxng"

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client
        self.inventory: dict | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            configured=bool(self.settings.searxng_http_url),
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
            self.settings.searxng_http_url.rstrip("/") + "/config",
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
            self.settings.searxng_http_url.rstrip("/") + "/search",
            data=params,
            headers={"Accept": "application/json"},
        )
        return normalize(data, self.name)


class RestProvider:
    def __init__(
        self,
        name: str,
        settings: Settings,
        client: httpx.AsyncClient,
        api_key: SecretStr | None = None,
    ):
        self.name, self.settings, self.client = name, settings, client
        keys = settings.provider_keys(name)
        self.api_key = api_key or (keys[0] if keys else None)

    def capabilities(self) -> ProviderCapabilities:
        configured = bool(self.api_key)
        supported = self.name == "tavily" or self.settings.serpapi_engine in {"google", "bing"}
        return ProviderCapabilities(
            name=self.name,
            configured=configured,
            categories=["general", "news"] if supported else [],
            time_ranges=["day", "week", "month", "year"] if supported else [],
            explicit_dates=supported,
            diagnostics=(
                ["bing_news_no_explicit_dates_or_year", "relative_publication_dates_remain_unknown"]
                if self.name == "serpapi" and self.settings.serpapi_engine == "bing"
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
        if self.name == "tavily":
            payload = {
                "query": request.query,
                "max_results": min(20, max(10, request.limit)),
                "topic": request.category,
                "search_depth": self.settings.tavily_depth,
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
                headers={"Authorization": "Bearer " + key},
            )
            return normalize(data, self.name)
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

    async def account_status(self) -> dict:
        if self.name != "serpapi" or not self.api_key:
            return {"status": "unconfigured"}
        data = await json_request(
            self.client,
            "GET",
            "https://serpapi.com/account.json",
            params={"api_key": self.api_key.get_secret_value()},
        )
        remaining = data.get("total_searches_left")
        remaining = remaining if type(remaining) is int and remaining >= 0 else None
        return {
            "status": "quota_exhausted" if remaining == 0 else "authenticated",
            "remaining_searches": remaining,
            "search_health": "not_checked",
        }

    async def quota_status(self) -> QuotaObservation:
        if not self.api_key or self.name not in {"serpapi", "tavily"}:
            raise ProviderError("unsupported_quota_discovery")
        if self.name == "serpapi":
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
                account_remaining = max(0, plan_limit - plan_usage)
                paygo_limit, paygo_usage = account.get("paygo_limit"), account.get("paygo_usage")
                if (
                    type(paygo_limit) is int
                    and type(paygo_usage) is int
                    and min(paygo_limit, paygo_usage) >= 0
                ):
                    account_remaining += max(0, paygo_limit - paygo_usage)
                remaining = account_remaining
        return QuotaObservation(remaining=remaining, unit="credits", scope="account")


def mcp_payload(result, provider: str, tool: str) -> dict:
    if result.isError:
        text = " ".join(block.text for block in result.content if block.type == "text")
        error_object({"error": text or "upstream error"})
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    text = "\n".join(block.text for block in result.content if block.type == "text")
    if len(text) > 5000000:
        raise ProviderError("malformed_response")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        return payload
    if provider == "exa":
        if text.strip() in {
            "No search results found. Please try a different query.",
            "No search results found. Please try a different query or adjust your filters.",
        }:
            return {"results": []}
        if tool == "web_search_exa":
            rows = []
            for block in text.split("\n\n---\n\n"):
                match = re.fullmatch(
                    r"Title: (.*?)\nURL: (\S+)\nPublished: (.*?)\nAuthor: [^\n]*(?:\n(?:Highlights:\n|Text: )(.*))?",
                    block,
                    re.DOTALL,
                )
                if not match:
                    raise ProviderError("malformed_response")
                title, url, publication, content = match.groups()
                rows.append(
                    {"title": title, "url": url, "publishedDate": publication, "content": content}
                )
            return {"results": rows}
    raise ProviderError("malformed_response")


class MCPProvider:
    def __init__(
        self, name: str, settings: Settings, client_factory=None, api_key: SecretStr | None = None
    ):
        if name != "exa":
            raise ValueError("outbound MCP is supported only for Exa")
        self.name, self.settings = name, settings
        keys = settings.provider_keys(name)
        self.api_key = api_key or (keys[0] if keys else None)
        self.client_factory = client_factory or httpx.AsyncClient
        self.tools: dict = {}
        self.discovery_status = "not_discovered"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            configured=bool(self.api_key or self.settings.exa_keyless),
            categories=["general", "news"],
            locales=[],
            time_ranges=["day", "week", "month", "year"],
            explicit_dates=True,
            diagnostics=[self.discovery_status],
        )

    def arguments(self, request: SearchRequest) -> tuple[str, dict]:
        if request.locale:
            raise unsupported(request, "locale", reason="language_filter_not_supported")
        advanced = bool(
            request.category == "news"
            or request.include_domains
            or request.exclude_domains
            or request.time_range
            or request.start_date
            or request.end_date
        )
        tool = "web_search_advanced_exa" if advanced else "web_search_exa"
        args = {"query": request.query, "numResults": max(10, request.limit)}
        if advanced:
            args.update(textMaxCharacters=2000, enableSummary=False)
            if request.category == "news":
                args["category"] = "news"
            for local, remote in (
                ("include_domains", "includeDomains"),
                ("exclude_domains", "excludeDomains"),
                ("start_date", "startPublishedDate"),
                ("end_date", "endPublishedDate"),
            ):
                value = getattr(request, local)
                if value:
                    args[remote] = str(value) if local.endswith("date") else value
            if request.time_range:
                days = {"day": 1, "week": 7, "month": 30, "year": 365}[request.time_range]
                args["startPublishedDate"] = (utcnow() - timedelta(days=days)).isoformat()
        return tool, args

    async def _session(self, operation):
        endpoint = self.settings.exa_url
        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key.get_secret_value()
        async with self.client_factory(
            headers=headers,
            timeout=self.settings.attempt_timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with streamable_http_client(endpoint, http_client=client) as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = {}
                    cursor = None
                    for page in range(20):
                        listing = await session.list_tools(cursor=cursor)
                        tools.update({tool.name: tool for tool in listing.tools})
                        cursor = listing.nextCursor
                        if not cursor:
                            break
                    else:
                        raise ProviderError("malformed_response")
                    self.tools = tools
                    self.discovery_status = "discovered"
                    return await operation(session)

    async def discover(self) -> dict:
        async def collect(session):
            return {
                "tools": [
                    {
                        "name": tool.name,
                        "input_schema": tool.inputSchema,
                        "output_schema": tool.outputSchema,
                    }
                    for tool in self.tools.values()
                ]
            }

        return await self._session(collect)

    async def search(self, request: SearchRequest, charge: Callable[[], Awaitable[None]]) -> Batch:
        if not self.capabilities().configured:
            raise ProviderError("unconfigured")
        tool_name, args = self.arguments(request)

        async def call(session):
            tool = self.tools.get(tool_name)
            if not tool:
                raise ProviderError(
                    "unsupported_capability",
                    diagnostics=[
                        CapabilityIssue(
                            field="tool", value=tool_name, reason="required_tool_not_advertised"
                        )
                    ],
                )
            properties = tool.inputSchema.get("properties", {})
            if not isinstance(properties, dict):
                raise ProviderError(
                    "unsupported_capability",
                    diagnostics=[
                        CapabilityIssue(
                            field="tool_schema",
                            reason="invalid_tool_schema",
                        )
                    ],
                )
            fields = {
                "query": "query",
                "numResults": "limit",
                "category": "category",
                "includeDomains": "include_domains",
                "excludeDomains": "exclude_domains",
                "startPublishedDate": "time_range" if request.time_range else "start_date",
                "endPublishedDate": "end_date",
            }

            def schema_issue(parameter, reason):
                local = fields.get(parameter)
                if local and local != "query":
                    return unsupported(request, local, reason=reason)
                safe_parameter = (
                    parameter
                    if isinstance(parameter, str)
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", parameter)
                    else "tool_schema"
                )
                return ProviderError(
                    "unsupported_capability",
                    diagnostics=[
                        CapabilityIssue(
                            field=safe_parameter,
                            value=(
                                str(args[parameter])
                                if parameter in {"textMaxCharacters", "enableSummary"}
                                else None
                            ),
                            reason=reason,
                        )
                    ],
                )

            if "objective" in properties:
                args["objective"] = (
                    "Find documents relevant to the query, exclude unrelated documents, and return supporting facts."
                )
            missing = [name for name in args if name not in properties]
            if missing:
                failure = schema_issue(missing[0], "parameter_not_advertised")
                for parameter in missing[1:]:
                    failure.diagnostics.extend(
                        schema_issue(parameter, "parameter_not_advertised").diagnostics
                    )
                raise failure

            def local_references(value):
                if isinstance(value, dict):
                    if "$ref" in value and not str(value["$ref"]).startswith("#"):
                        return False
                    return all(local_references(item) for item in value.values())
                if isinstance(value, list):
                    return all(local_references(item) for item in value)
                return True

            if not local_references(tool.inputSchema):
                raise schema_issue(None, "external_schema_reference_not_supported")
            try:
                jsonschema.Draft202012Validator(tool.inputSchema).validate(args)
            except jsonschema.ValidationError as error:
                parameter = next(iter(error.absolute_path), None)
                if error.validator == "required" and not parameter:
                    missing = [name for name in error.validator_value if name not in args]
                    failure = ProviderError("unsupported_capability")
                    for parameter in missing:
                        failure.diagnostics.extend(
                            schema_issue(parameter, "required_parameter_not_supported").diagnostics
                        )
                    raise failure from None
                raise schema_issue(parameter, "schema_value_not_supported") from None
            except jsonschema.SchemaError:
                raise schema_issue(None, "invalid_tool_schema") from None
            await charge()
            result = await session.call_tool(tool_name, args)
            payload = mcp_payload(result, self.name, tool_name)
            return normalize(payload, self.name)

        return await self._session(call)


def classify_exception(error: Exception) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            if isinstance(child, Exception):
                result = classify_exception(child)
                if result.reason != "internal_error":
                    return result
    if isinstance(error, httpx.HTTPStatusError):
        try:
            check_http(error.response)
        except ProviderError as result:
            return result
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return ProviderError("timeout")
    if isinstance(error, (httpx.TransportError, OSError)):
        return ProviderError("transport")
    return ProviderError("internal_error")
