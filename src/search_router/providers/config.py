from typing import Annotated, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

KeyName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$"
    ),
]


def validate_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("invalid administrator endpoint")
    if any(key in parsed.query.casefold() for key in ("key=", "token=", "secret=")):
        raise ValueError("credentials belong in headers, not endpoint URLs")
    return value


def secret_values(value) -> list[str]:
    if isinstance(value, SecretStr):
        return [value.get_secret_value()] if value.get_secret_value() else []
    if isinstance(value, BaseModel):
        return secret_values({name: getattr(value, name) for name in type(value).model_fields})
    if isinstance(value, dict):
        return [secret for item in value.values() for secret in secret_values(item)]
    if isinstance(value, (list, tuple)):
        return [secret for item in value for secret in secret_values(item)]
    return []


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    enabled: bool = False
    estimated_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    hourly_attempt_limit: int | None = Field(default=None, ge=0)
    quota_unit: ClassVar[str] = "unknown"

    def credentials(self) -> list[SecretStr]:
        return []

    def cache_identity(self) -> dict:
        return self.model_dump(mode="json")

    def quota_identity(self) -> dict:
        return {}


class CredentialConfig(ProviderConfig):
    keys: list[SecretStr] = Field(default_factory=list, exclude=True, repr=False)
    key_names: list[KeyName] = Field(default_factory=list, exclude=True, repr=False)
    key_strategy: Literal["round_robin", "failover", "random", "least_inflight"] = "round_robin"
    quota_scope: Literal["independent", "shared"] = "independent"

    @field_validator("keys")
    @classmethod
    def unique_keys(cls, values: list[SecretStr]) -> list[SecretStr]:
        return [
            SecretStr(value)
            for value in dict.fromkeys(key.get_secret_value().strip() for key in values)
            if value
        ]

    def credentials(self) -> list[SecretStr]:
        return list(self.keys)

    @model_validator(mode="after")
    def populate_key_names(self):
        if len(self.key_names) > len(self.keys):
            raise ValueError("key names must correspond to configured keys")
        self.key_names = [
            *self.key_names,
            *(f"Key {index + 1}" for index in range(len(self.key_names), len(self.keys))),
        ]
        return self
