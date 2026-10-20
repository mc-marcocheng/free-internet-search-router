from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .config import Settings
from .providers.config import KeyName
from .storage import ConfigurationError, ConfigurationStore, settings_tree


class ConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(ge=1)
    configuration: dict


class CredentialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(ge=1)
    operation: Literal["add", "replace", "remove", "rename"]
    provider: str | None = None
    field: Literal["service_key"] | None = None
    token_name: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    index: int | None = Field(default=None, ge=1)
    name: KeyName | None = None
    value: SecretStr | None = Field(default=None, exclude=True, repr=False, max_length=16384)


class Management:
    def __init__(self, store: ConfigurationStore):
        self.store = store

    def configure(self, update: ConfigurationUpdate) -> int:
        revision, current = self.store.load()
        if revision != update.revision:
            raise ConfigurationError("configuration_conflict")
        return self.store.save(self.store.candidate(update.configuration, current), revision)

    def credential(self, update: CredentialUpdate) -> int:
        revision, current = self.store.load()
        if revision != update.revision:
            raise ConfigurationError("configuration_conflict")
        if bool(update.provider) == bool(update.token_name):
            raise ConfigurationError("choose_provider_or_token")
        if update.operation in {"add", "replace"} and (
            update.value is None or not update.value.get_secret_value().strip()
        ):
            raise ConfigurationError("credential_required")
        tree = settings_tree(current)
        if update.field:
            provider = tree["providers"].get(update.provider, {})
            original = provider.get(update.field)
            if (
                not isinstance(original, SecretStr)
                or update.operation == "rename"
                or update.index is not None
                or update.name is not None
            ):
                raise ConfigurationError("invalid_credential_operation")
            exists = bool(original.get_secret_value())
            if (update.operation == "add" and exists) or (update.operation != "add" and not exists):
                raise ConfigurationError("invalid_credential_operation")
            provider[update.field] = SecretStr("") if update.operation == "remove" else update.value
        elif update.token_name:
            if update.operation == "rename" or update.name is not None:
                raise ConfigurationError("invalid_token_operation")
            tokens = tree["tokens"]
            exists = update.token_name in tokens
            if (update.operation == "add" and exists) or (update.operation != "add" and not exists):
                raise ConfigurationError("invalid_token_operation")
            if update.operation == "remove":
                del tokens[update.token_name]
            else:
                tokens[update.token_name] = update.value
        else:
            provider = tree["providers"].get(update.provider)
            if provider is None or "keys" not in provider:
                raise ConfigurationError("provider_has_no_key_pool")
            keys = provider["keys"]
            key_names = provider["key_names"]
            usage_ids = provider.get("usage_key_ids")
            if update.operation == "add":
                if any(
                    key.get_secret_value() == update.value.get_secret_value().strip()
                    for key in keys
                ):
                    raise ConfigurationError("duplicate_credential")
                keys.append(update.value)
                key_names.append(update.name or f"Key {len(keys)}")
                if usage_ids is not None:
                    usage_ids.append("")
            else:
                if update.index is None or update.index > len(keys):
                    raise ConfigurationError("unknown_key_index")
                if update.operation == "replace" and any(
                    index != update.index - 1
                    and key.get_secret_value() == update.value.get_secret_value().strip()
                    for index, key in enumerate(keys)
                ):
                    raise ConfigurationError("duplicate_credential")
                if update.operation == "remove":
                    keys.pop(update.index - 1)
                    key_names.pop(update.index - 1)
                    if usage_ids is not None:
                        usage_ids.pop(update.index - 1)
                elif update.operation == "rename":
                    if update.name is None:
                        raise ConfigurationError("key_name_required")
                    key_names[update.index - 1] = update.name
                else:
                    keys[update.index - 1] = update.value
                    if usage_ids is not None:
                        usage_ids[update.index - 1] = ""
                    if update.name is not None:
                        key_names[update.index - 1] = update.name
        try:
            candidate = Settings(registry=current.registry, **tree)
        except Exception:
            raise ConfigurationError("invalid_configuration") from None
        return self.store.save(candidate, revision)
