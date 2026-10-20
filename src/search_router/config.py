import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .providers.config import ProviderConfig, secret_values
from .providers.registry import ProviderRegistry, builtin_registry
from .search.policy import AcceptancePolicy


class BootstrapSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROUTER_", env_file=None, extra="ignore")
    database: Path = Field(
        default_factory=lambda: (
            Path(
                os.environ.get("LOCALAPPDATA")
                or os.environ.get("XDG_DATA_HOME")
                or Path.home() / ".local/share"
            )
            / "internet-search-router/config.sqlite3"
        )
    )
    host: str = "127.0.0.1"
    port: int = Field(default=9092, ge=1, le=65535)
    key_file: Path | None = None


class Settings(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )
    registry: ProviderRegistry = Field(default_factory=builtin_registry, exclude=True, repr=False)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict, repr=False)
    tokens: dict[str, SecretStr] = Field(default_factory=dict, exclude=True, repr=False)
    administrators: list[str] = Field(default_factory=lambda: ["admin"])
    provider_order: list[str] = ["searxng", "exa", "serpapi", "tavily"]
    profiles: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "economy": ["searxng"],
            "quality": ["searxng", "exa", "serpapi", "tavily"],
        }
    )
    policy: AcceptancePolicy = Field(default_factory=AcceptancePolicy)
    policy_version: str = "1"
    total_timeout: float = Field(default=45, gt=0, le=300)
    attempt_timeout: float = Field(default=10, gt=0, allow_inf_nan=False)
    cache_ttl: float = Field(default=300, ge=0)
    cache_entries: int = Field(default=512, ge=1)
    max_concurrent_searches: int = Field(default=32, ge=1)
    max_attempts_per_request: int = Field(default=4, ge=1)
    hourly_attempt_limit: int = Field(default=1000, ge=0)
    hourly_cost_limit_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    circuit_threshold: int = Field(default=3, ge=1)
    cooldown_seconds: float = Field(default=60, ge=0)
    key_max_attempts: int = Field(default=2, ge=1, le=8)
    key_quota_cooldown_seconds: float = Field(default=3600, gt=0, allow_inf_nan=False)
    quota_cache_ttl_seconds: float = Field(default=900, gt=0, allow_inf_nan=False)
    quota_cache_enabled: bool = True
    quota_cache_dir: str | None = None
    quota_refresh_enabled: bool = True
    quota_refresh_hourly_limit: int = Field(default=60, ge=0)
    reader_limit_bytes: int = Field(default=5000000, ge=1024, le=20000000)
    reader_timeout: float = Field(default=20, gt=0, le=45)
    reader_concurrency: int = Field(default=4, ge=1)
    reader_max_pages: int = Field(default=100, ge=1)
    reader_node_executable: str = "node"
    reader_backends: list[Literal["httpx", "cloakbrowser"]] = ["httpx", "cloakbrowser"]
    reader_backend_timeout: float = Field(default=12, gt=0, le=45)
    otlp_endpoint: str | None = None
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]", "testserver"]

    @model_validator(mode="before")
    @classmethod
    def resolve_providers(cls, values):
        values = dict(values)
        if any(key not in cls.model_fields for key in values):
            raise ValueError("unknown router setting; use grouped provider configuration")
        registry = values.get("registry") or builtin_registry()
        if not isinstance(registry, ProviderRegistry):
            raise ValueError("invalid provider registry")
        values["registry"] = registry
        values["providers"] = registry.configurations(values.get("providers", {}))
        return values

    @model_validator(mode="after")
    def valid_policy(self) -> "Settings":
        if not self.reader_backends or len(set(self.reader_backends)) != len(self.reader_backends):
            raise ValueError("reader backends must be nonempty and unique")
        for chain in [self.provider_order, *self.profiles.values()]:
            if len(chain) != len(set(chain)) or not set(chain) <= self.providers.keys():
                raise ValueError("provider chains must contain unique registered names")
        if any(not name.strip() or name == "default" for name in self.profiles):
            raise ValueError("profile names must be nonblank and cannot replace default")
        return self

    def secrets(self) -> list[str]:
        return list(dict.fromkeys(secret_values([self.tokens, *self.providers.values()])))
