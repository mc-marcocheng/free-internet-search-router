import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections import deque
from pathlib import Path

from pydantic import Field

from .models import Contract, QuotaObservation
from .providers import classify_exception


class CacheEntry(Contract):
    quota: QuotaObservation | None = None
    blocked_until: float = Field(default=0, ge=0, le=4102444800, allow_inf_nan=False)
    retry_until: float = Field(default=0, ge=0, le=4102444800, allow_inf_nan=False)
    pool_blocked_until: float = Field(default=0, ge=0, le=4102444800, allow_inf_nan=False)
    pool_retry_until: float = Field(default=0, ge=0, le=4102444800, allow_inf_nan=False)
    next_refresh_at: float = Field(default=0, ge=0, le=4102444800, allow_inf_nan=False)
    exhausted: bool = Field(default=False, strict=True)


class QuotaCache(Contract):
    version: int = Field(default=2, ge=2, le=2, strict=True)
    entries: dict[str, CacheEntry] = Field(default_factory=dict, max_length=10000)


class QuotaTracker:
    def __init__(self, settings, pools, wall_clock=time.time):
        self.settings, self.pools, self.wall_clock = settings, pools, wall_clock
        self.event = asyncio.Event()
        self.task = None
        self.dirty = False
        self.storage_status = "disabled" if not settings.quota_cache_enabled else "not_loaded"
        self.probes = deque()
        self.probe_attempts = 0
        self.identities = {}
        for pool in pools:
            keys = pool.config.credentials()
            for index, key in enumerate(keys):
                if index >= len(pool.states):
                    break
                identity = json.dumps(
                    [
                        "search-router-quota-v2",
                        pool.name,
                        pool.config.cache_identity(),
                        key.get_secret_value(),
                        pool.config.quota_scope,
                    ]
                )
                self.identities[pool, index] = hashlib.sha256(identity.encode()).hexdigest()
        self.path = self._path()

    def _path(self):
        if self.settings.quota_cache_dir:
            return Path(self.settings.quota_cache_dir) / "quota-cache.json"
        base = (
            os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_CACHE_HOME")
        )
        return (Path(base) if base else Path.home() / ".cache") / (
            "internet-search-router/quota-cache.json"
        )

    def changed(self):
        self.dirty = True
        self.event.set()

    async def start(self):
        if self.settings.quota_cache_enabled:
            await self.load()
        for pool in self.pools:
            pool.on_change = self.changed
            pool.on_refresh = self.event.set
        self.task = asyncio.create_task(self._run())

    async def load(self):
        def read():
            if not self.path.exists():
                return QuotaCache()
            with self.path.open("rb") as stream:
                content = stream.read(2_000_001)
            if len(content) > 2_000_000:
                raise ValueError("cache_too_large")
            return QuotaCache.model_validate_json(content)

        try:
            cache = await asyncio.to_thread(read)
        except (OSError, ValueError):
            self.storage_status = "unavailable"
            return
        self.storage_status = "ready"
        now = self.wall_clock()
        for (pool, index), identity in self.identities.items():
            entry = cache.entries.get(identity)
            if entry is None:
                continue
            state = pool.states[index]
            state.quota, state.exhausted = entry.quota, entry.exhausted
            state.blocked_until = pool.clock() + max(0, entry.blocked_until - now)
            state.retry_until = pool.clock() + max(0, entry.retry_until - now)
            pool.blocked_until = max(
                pool.blocked_until, pool.clock() + max(0, entry.pool_blocked_until - now)
            )
            pool.retry_until = max(
                pool.retry_until, pool.clock() + max(0, entry.pool_retry_until - now)
            )
            state.next_refresh_at = min(
                entry.next_refresh_at, now + self.settings.quota_cache_ttl_seconds
            )

    async def flush(self):
        if not self.dirty or not self.settings.quota_cache_enabled:
            return
        entries = {}
        now = self.wall_clock()
        for (pool, index), identity in self.identities.items():
            state = pool.states[index]
            entries[identity] = CacheEntry(
                quota=state.quota,
                exhausted=state.exhausted,
                blocked_until=now + max(0, state.blocked_until - pool.clock()),
                retry_until=now + max(0, state.retry_until - pool.clock()),
                pool_blocked_until=now + max(0, pool.blocked_until - pool.clock()),
                pool_retry_until=now + max(0, pool.retry_until - pool.clock()),
                next_refresh_at=state.next_refresh_at,
            )
        content = QuotaCache(entries=entries).model_dump_json()
        self.dirty = False

        def write():
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".quota-")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        try:
            write_task = asyncio.create_task(asyncio.to_thread(write))
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                await asyncio.gather(write_task, return_exceptions=True)
                raise
            self.storage_status = "ready"
        except (OSError, ValueError):
            self.storage_status = "unavailable"
            self.dirty = True

    async def refresh(self, pool, index):
        state = pool.states[index]
        generation = state.generation
        self.probe_attempts += 1
        self.probes.append(self.wall_clock())
        try:
            async with asyncio.timeout(self.settings.attempt_timeout):
                quota = await pool.providers[index].quota_status()
            async with pool.lock:
                if generation == state.generation:
                    pool.observe_quota(index, quota)
            state.refresh_status = "completed"
            state.refresh_failures = 0
            delay = self.settings.quota_cache_ttl_seconds
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = classify_exception(error)
            state.refresh_status = failure.reason
            state.refresh_failures = min(10, state.refresh_failures + 1)
            delay = max(
                failure.retry_after or 0,
                min(self.settings.quota_cache_ttl_seconds, 60 * 2 ** (state.refresh_failures - 1)),
            )
        state.next_refresh_at = self.wall_clock() + delay
        self.changed()

    async def _run(self):
        while True:
            self.event.clear()
            now = self.wall_clock()
            while self.probes and self.probes[0] <= now - 3600:
                self.probes.popleft()
            due = sorted(
                (
                    (state.next_refresh_at, pool.name, index, pool)
                    for pool in self.pools
                    for index, state in enumerate(pool.states)
                    if callable(getattr(pool.providers[index], "quota_status", None))
                    and pool.config.enabled
                ),
                key=lambda item: item[:3],
            )
            allowed = (
                self.settings.quota_refresh_enabled
                and len(self.probes) < self.settings.quota_refresh_hourly_limit
            )
            if allowed and due and due[0][0] <= now:
                _, _, index, pool = due[0]
                await self.refresh(pool, index)
                await self.flush()
                continue
            await self.flush()
            delay = max(0.01, due[0][0] - now) if allowed and due else 60
            try:
                async with asyncio.timeout(min(60, delay)):
                    await self.event.wait()
            except TimeoutError:
                pass

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for pool in self.pools:
            pool.on_change = lambda: None
            pool.on_refresh = lambda: None
        await self.flush()
