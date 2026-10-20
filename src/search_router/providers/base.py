from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..contracts import (
    CapabilityIssue,
    KeyAttempt,
    ProviderCapabilities,
    QuotaObservation,
    SearchRequest,
    SearchResult,
    Usage,
)

Charge = Callable[[], Awaitable[None]]


class ProviderError(Exception):
    def __init__(
        self,
        reason: str,
        retry_after: float | None = None,
        diagnostics: list[CapabilityIssue] | None = None,
    ):
        self.reason = reason
        self.retry_after = retry_after
        self.key_attempts: list[KeyAttempt] = []
        self.diagnostics = diagnostics or []
        super().__init__(reason)


@dataclass
class Batch:
    results: list[SearchResult]
    upstream_count: int
    warnings: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    key_attempts: list[KeyAttempt] = field(default_factory=list)


class Provider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    async def search(self, request: SearchRequest, charge: Charge) -> Batch: ...


@runtime_checkable
class Discoverable(Protocol):
    async def discover(self) -> dict: ...


@runtime_checkable
class QuotaSource(Protocol):
    async def quota_status(self) -> QuotaObservation: ...


@runtime_checkable
class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...
