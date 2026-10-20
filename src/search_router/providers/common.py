import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..contracts import (
    CapabilityIssue,
    SearchRequest,
    SearchResult,
    Usage,
    utcnow,
)
from .base import Batch, ProviderError


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


def normalize(payload: dict, provider: str, key="results", *, engine_warnings=True) -> Batch:
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
    if engine_warnings and payload.get("unresponsive_engines"):
        warnings.append("some_engines_unresponsive")
    credits = payload.get("usage", {})
    credits = credits.get("credits") if isinstance(credits, dict) else None
    if not isinstance(credits, (float, int)) or isinstance(credits, bool) or credits < 0:
        credits = None
    return Batch(
        results=results, upstream_count=len(rows), warnings=warnings, usage=Usage(credits=credits)
    )


async def json_request(
    client: httpx.AsyncClient, method: str, url: str, *, prepare=None, **kwargs
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
        if prepare is not None:
            prepare(payload)
        error_object(payload)
        return payload


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
