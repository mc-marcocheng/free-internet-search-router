import math
from email.utils import parsedate_to_datetime

import httpx

from ..contracts import utcnow
from .base import ProviderError


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
