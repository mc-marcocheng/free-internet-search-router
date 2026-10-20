from datetime import date, datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (BaseModel, ConfigDict, Field, JsonValue, RootModel,
                      field_validator, model_validator)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuotaObservation(Contract):
    remaining: (
        Annotated[int, Field(ge=0, strict=True)]
        | Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)]
        | None
    ) = None
    unit: Literal["searches", "credits", "usd", "unknown"]
    observed_at: datetime = Field(default_factory=utcnow)
    renewal_date: date | None = None
    reset_at: datetime | None = None
    scope: Literal["key", "account"] = "key"
    source: Literal["account_api", "search_error", "configured_budget"] = "account_api"
    budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    used_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def integral_counts(self):
        if self.unit != "usd" and self.remaining is not None and type(self.remaining) is not int:
            raise ValueError("non-monetary quota must be an integer")
        return self

    @field_validator("observed_at", "reset_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("quota timestamps must include a timezone")
        return value


class SearchRequest(Contract):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=50)
    category: Literal["general", "news"] = "general"
    locale: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4}){0,2}$")
    include_domains: list[str] = Field(default_factory=list, max_length=100)
    exclude_domains: list[str] = Field(default_factory=list, max_length=100)
    time_range: Literal["day", "week", "month", "year"] | None = None
    start_date: date | None = None
    end_date: date | None = None
    profile: str = Field(default="default", min_length=1, max_length=64, pattern=r"^\S+$")

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def domain_names(cls, values: list[str]) -> list[str]:
        import re

        normalized = []
        for value in values:
            value = value.lower().rstrip(".").encode("idna").decode("ascii")
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
                raise ValueError("expected domain names, not URLs")
            normalized.append(value)
        return sorted(set(normalized))

    @model_validator(mode="after")
    def date_window(self) -> "SearchRequest":
        if self.time_range and (self.start_date or self.end_date):
            raise ValueError("time_range and explicit dates are mutually exclusive")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must precede end_date")
        return self


class SearchResult(Contract):
    url: str
    title: str
    snippet: str
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    provider: str
    engines: list[str] = Field(default_factory=list)


class Usage(Contract):
    upstream_attempts: int = 0
    credits: float | None = None
    estimated_cost_usd: float | None = None
    billed_cost_usd: float | None = None


class CapabilityIssue(Contract):
    field: str
    value: str | list[str] | None = None
    reason: str


class KeyAttempt(Contract):
    key_index: int
    outcome: Literal["completed", "failed", "skipped"]
    reason: str
    diagnostics: list[CapabilityIssue] = Field(default_factory=list)
    latency_ms: float = 0
    usage: Usage = Field(default_factory=Usage)


class Attempt(Contract):
    provider: str
    outcome: Literal["accepted", "partial", "empty", "failed", "skipped"]
    reason: str
    diagnostics: list[CapabilityIssue] = Field(default_factory=list)
    latency_ms: float = 0
    valid_results: int = 0
    contributing_engines: int = 0
    key_attempts: list[KeyAttempt] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)


class CacheMetadata(Contract):
    hit: bool = False
    coalesced: bool = False
    age_seconds: float = 0
    ttl_seconds: float = 300
    origin_request_id: str | None = None


class SearchResponse(Contract):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    effective_request: SearchRequest
    status: Literal["ok", "partial", "empty", "unavailable"]
    results: list[SearchResult] = Field(default_factory=list)
    selected_provider: str | None = None
    warnings: list[str] = Field(default_factory=list)
    cache: CacheMetadata = Field(default_factory=CacheMetadata)
    attempts: list[Attempt] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    policy_version: str = "1"


class ReadRequest(Contract):
    url: str = Field(min_length=1, max_length=4096)
    max_chars: int = Field(default=100000, ge=1, le=1000000)
    minimal: bool = True


class ReadResponse(Contract):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["ok", "unsupported", "unavailable", "blocked"]
    original_url: str
    final_url: str | None = None
    text: str | None = None
    content_type: str | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


class ProviderCapabilities(Contract):
    name: str
    configured: bool
    categories: list[str] = Field(default_factory=list)
    locales: list[str] | None = None
    time_ranges: list[str] = Field(default_factory=list)
    explicit_dates: bool = False
    domains: bool = True
    engines: list[str] | None = None
    diagnostics: list[str] = Field(default_factory=list)


class Capabilities(Contract):
    schema_version: Literal["1.0"] = "1.0"
    providers: list[ProviderCapabilities]
    profiles: dict[str, list[str]]
    reader_types: list[str] = ["text/html", "text/plain", "application/pdf"]
    reader_limit_bytes: int
    policy_version: str


class ValidationIssue(Contract):
    location: list[str | int]
    type: str


class ErrorResponse(Contract):
    error: str
    fields: list[ValidationIssue] = Field(default_factory=list)


class KeyStatus(Contract):
    key_index: int
    inflight: int
    upstream_attempts: int
    last_reason: str | None
    cooldown_remaining_seconds: float
    available: bool
    quota: QuotaObservation | None
    quota_stale: bool
    quota_discovery_supported: bool
    quota_refresh_status: str


class KeyPoolStatus(Contract):
    strategy: str
    key_count: int
    keys: list[KeyStatus]


class ProviderStatus(Contract):
    name: str
    enabled: bool
    capabilities: ProviderCapabilities
    operations: list[str]
    account_health: str
    search_health: str
    last_reason: str | None
    consecutive_infrastructure_failures: int
    cooldown_remaining_seconds: float
    key_pool: KeyPoolStatus | None


class QuotaCacheStatus(Contract):
    storage_status: str
    probe_attempts: int
    hourly_probe_limit: int


class ProviderList(Contract):
    logical_requests: int
    actual_upstream_attempts: int
    budget_window_seconds: int
    reserved_estimated_cost_usd: float
    providers: dict[str, ProviderStatus]
    quota_cache: QuotaCacheStatus


class ProviderDiscovery(Contract):
    provider: str
    details: dict[str, JsonValue]


class ProviderQuotaStatus(Contract):
    provider: str
    configured: bool
    keys: list[KeyStatus]
    quota_cache: QuotaCacheStatus


class SearchOutcome(RootModel[SearchResponse | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class ProviderOutcome(RootModel[ProviderStatus | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class DiscoveryOutcome(RootModel[ProviderDiscovery | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class QuotaOutcome(RootModel[ProviderQuotaStatus | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})
