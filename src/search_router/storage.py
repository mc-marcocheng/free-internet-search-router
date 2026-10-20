import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, SecretStr

from .config import Settings
from .protected import SecretCipher
from .providers.config import CredentialConfig
from .providers.registry import ProviderRegistry, builtin_registry


class ConfigurationError(Exception):
    pass


def settings_tree(value):
    if isinstance(value, BaseModel):
        return {
            name: settings_tree(getattr(value, name))
            for name in type(value).model_fields
            if name != "registry"
        }
    if isinstance(value, dict):
        return {name: settings_tree(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [settings_tree(item) for item in value]
    return value


class ConfigurationStore:
    def __init__(self, path: Path, cipher: SecretCipher, registry: ProviderRegistry | None = None):
        self.path, self.cipher = Path(path), cipher
        self.registry = registry or builtin_registry()

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
            yield connection
        except (sqlite3.Error, OSError):
            raise ConfigurationError("configuration_storage_unavailable") from None
        finally:
            connection.close()

    def initialize(self, settings: Settings):
        with self.connection() as connection, connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ConfigurationError("unsupported_configuration_version")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS configuration "
                "(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, document TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, sealed BLOB NOT NULL)"
            )
            connection.execute("PRAGMA user_version=1")
            if connection.execute("SELECT 1 FROM configuration").fetchone():
                raise ConfigurationError("configuration_already_initialized")
            self._write(connection, settings, 1)

    def _write(self, connection, settings: Settings, revision: int):
        connection.execute("DELETE FROM credentials")

        def encode(value):
            if isinstance(value, SecretStr):
                identity = str(uuid4())
                connection.execute(
                    "INSERT INTO credentials VALUES (?, ?)",
                    (identity, self.cipher.encrypt(identity, value.get_secret_value())),
                )
                return {"$secret": identity}
            if isinstance(value, dict):
                return {name: encode(item) for name, item in value.items()}
            if isinstance(value, list):
                return [encode(item) for item in value]
            return value

        document = json.dumps(encode(settings_tree(settings)), allow_nan=False)
        connection.execute(
            "INSERT INTO configuration VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, document=excluded.document",
            (revision, document),
        )

    def _load(self, connection) -> tuple[int, Settings]:
        try:
            row = connection.execute(
                "SELECT revision, document FROM configuration WHERE id=1"
            ).fetchone()
            if row is None:
                raise ConfigurationError("configuration_not_initialized")
            credentials = dict(connection.execute("SELECT id, sealed FROM credentials"))

            def decode(value):
                if isinstance(value, dict):
                    if set(value) == {"$secret"}:
                        identity = value["$secret"]
                        return self.cipher.decrypt(identity, credentials[identity])
                    return {name: decode(item) for name, item in value.items()}
                if isinstance(value, list):
                    return [decode(item) for item in value]
                return value

            return row[0], Settings(registry=self.registry, **decode(json.loads(row[1])))
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("invalid_or_locked_configuration") from None

    def load(self) -> tuple[int, Settings]:
        with self.connection() as connection:
            connection.execute("BEGIN")
            return self._load(connection)

    def public(self) -> dict:
        revision, settings = self.load()

        def public_tree(value):
            if isinstance(value, dict):
                return {
                    name: public_tree(item)
                    for name, item in value.items()
                    if not isinstance(item, SecretStr)
                    and name not in {"tokens", "keys", "key_names"}
                }
            if isinstance(value, list):
                return [public_tree(item) for item in value if not isinstance(item, SecretStr)]
            return value

        tree = settings_tree(settings)
        return {
            "revision": revision,
            "configuration": public_tree(tree),
            "credentials": {
                name: len(config.credentials()) for name, config in settings.providers.items()
            },
            "key_metadata": {
                name: [
                    {
                        "index": index + 1,
                        "name": config.key_names[index],
                        "preview": f"{value[:4]}...{value[-4:]}"
                        if len(value) >= 20
                        else "********",
                    }
                    for index, value in enumerate(
                        key.get_secret_value() for key in config.credentials()
                    )
                ]
                for name, config in settings.providers.items()
                if isinstance(config, CredentialConfig)
            },
            "tokens": sorted(settings.tokens),
            "provider_credentials": {
                name: {
                    field: bool(value.get_secret_value())
                    for field, value in values.items()
                    if isinstance(value, SecretStr)
                }
                for name, values in tree["providers"].items()
            },
            "provider_schemas": {
                name: spec.config_type.model_json_schema()
                for name, spec in self.registry.specs.items()
            },
        }

    def save(self, settings: Settings, expected_revision: int) -> int:
        if not settings.tokens or any(
            len(token.get_secret_value()) < 24 or "REPLACE_" in token.get_secret_value()
            for token in settings.tokens.values()
        ):
            raise ConfigurationError("valid_router_token_required")
        if not settings.administrators or any(
            name not in settings.tokens for name in settings.administrators
        ):
            raise ConfigurationError("valid_administrator_required")
        with self.connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current, _ = self._load(connection)
            if current != expected_revision:
                raise ConfigurationError("configuration_conflict")
            self._write(connection, settings, current + 1)
        return current + 1

    def candidate(self, configuration: dict, current: Settings) -> Settings:
        def preserve(incoming, original):
            if isinstance(original, SecretStr):
                if incoming is not None:
                    raise ConfigurationError("use_credential_operations")
                return original
            if isinstance(original, dict):
                incoming = dict(incoming or {})
                for name, item in original.items():
                    if name in {"tokens", "keys", "key_names"}:
                        if name in incoming:
                            raise ConfigurationError("use_credential_operations")
                        incoming[name] = item
                    elif isinstance(item, (dict, SecretStr)):
                        incoming[name] = preserve(incoming.get(name), item)
                return incoming
            return incoming

        try:
            merged = preserve(configuration, settings_tree(current))
            return Settings(registry=self.registry, **merged)
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("invalid_configuration") from None


def open_store(bootstrap, create=False) -> ConfigurationStore:
    from .protected import system_cipher

    if not create and not bootstrap.database.is_file():
        raise ConfigurationError("run_search_router_init_first")
    return ConfigurationStore(
        bootstrap.database, system_cipher(bootstrap.database, bootstrap.key_file, create=create)
    )
