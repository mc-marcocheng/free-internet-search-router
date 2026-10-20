import argparse
import getpass
import ipaddress
import json
import sys
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .api.app import create_app
from .config import BootstrapSettings, Settings
from .key_checks import KeyAction
from .management import ConfigurationUpdate, CredentialUpdate, Management
from .storage import ConfigurationError, open_store


def prompt_secret(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def remote_key_operation(bootstrap, store, arguments):
    revision, settings = store.load()
    address = arguments.url or f"http://127.0.0.1:{bootstrap.port}"
    parsed = urlsplit(address)
    try:
        loopback = (
            parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        )
    except ValueError:
        loopback = False
    if (
        parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and not loopback)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("secure_server_address_required")
    token = settings.tokens[settings.administrators[0]].get_secret_value()
    with httpx.Client(
        base_url=address,
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        follow_redirects=False,
        timeout=settings.attempt_timeout + 15,
    ) as client:
        if arguments.operation == "status":
            response = client.get(f"/admin/api/keys/{arguments.name}")
        else:
            action = KeyAction(
                revision=revision,
                provider=arguments.name,
                index=arguments.index,
                action="test" if arguments.operation == "test" else "refresh_quota",
                confirm_live=arguments.confirm_live,
                max_cost_usd=getattr(arguments, "max_cost_usd", None),
            )
            response = client.post("/admin/api/key-actions", json=action.model_dump(mode="json"))
        response.raise_for_status()
        result = response.json()
    print(json.dumps(result, indent=2))
    if arguments.operation != "status" and result.get("status") not in {"working", "refreshed"}:
        raise SystemExit(1)


def main():
    import uvicorn

    parser = argparse.ArgumentParser(
        description="Free Internet Search Router: REST and MCP for agents"
    )
    parser.add_argument("--database", type=Path, help="SQLite database path")
    parser.add_argument("--key-file", type=Path, help="External base64 master-key file")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser(
        "init", help="Initialize protected settings and administrator authentication"
    )
    commands.add_parser("serve", help="Start REST, MCP and the management UI")
    config = commands.add_parser("config", help="Manage validated non-secret settings")
    config_commands = config.add_subparsers(dest="operation", required=True)
    config_commands.add_parser("show", help="Show configuration and credential metadata")
    setting = config_commands.add_parser(
        "set", help="Set a dotted configuration path to a JSON value"
    )
    setting.add_argument("path")
    setting.add_argument("value")
    apply = config_commands.add_parser(
        "apply", help="Apply the non-secret configuration object from a JSON file"
    )
    apply.add_argument("file", type=Path)
    for kind in ("key", "token", "service-key"):
        credential = commands.add_parser(kind, help=f"Manage protected {kind}s")
        operations = credential.add_subparsers(dest="operation", required=True)
        for operation in ("add", "replace", "remove", *(("rename",) if kind == "key" else ())):
            command = operations.add_parser(operation)
            command.add_argument(
                "name",
                help="Token name" if kind == "token" else "Provider ID",
                **({"choices": ["exa"]} if kind == "service-key" else {}),
            )
            if kind == "key" and operation != "add":
                command.add_argument("index", type=int, help="One-based key index")
            if kind == "key" and operation in {"add", "replace", "rename"}:
                command.add_argument(
                    "--name",
                    dest="key_name",
                    required=operation == "rename",
                    help="Key display name",
                )
        if kind == "key":
            for operation in ("status", "test", "refresh-quota"):
                command = operations.add_parser(
                    operation, help="Use the running server's key diagnostics"
                )
                command.add_argument("name", help="Provider ID")
                command.add_argument("--url", help="Server base URL; HTTPS or loopback HTTP")
                if operation != "status":
                    command.add_argument("index", type=int, help="One-based key index")
                    command.add_argument(
                        "--confirm-live",
                        action="store_true",
                        required=True,
                        help="Authorize one upstream request",
                    )
                if operation == "test":
                    command.add_argument(
                        "--max-cost-usd",
                        type=float,
                        required=True,
                        help="Per-call estimated cost ceiling (0 to 1 USD)",
                    )
    arguments = parser.parse_args()
    try:
        overrides = {
            name: getattr(arguments, name)
            for name in ("database", "key_file")
            if getattr(arguments, name) is not None
        }
        bootstrap = BootstrapSettings(**overrides)
        if arguments.command == "init":
            if bootstrap.database.exists():
                raise ConfigurationError("configuration_already_exists")
            token = prompt_secret("Administrator token (at least 24 characters): ")
            if len(token) < 24 or "REPLACE_" in token:
                raise ConfigurationError("valid_router_token_required")
            if token != prompt_secret("Confirm administrator token: "):
                raise ConfigurationError("token_confirmation_mismatch")
            open_store(bootstrap, create=True).initialize(Settings(tokens={"admin": token}))
            print("Configuration initialized. Start search-router serve, then open /admin.")
            return
        store = open_store(bootstrap)
        management = Management(store)
        if arguments.command == "key" and arguments.operation in {
            "status",
            "test",
            "refresh-quota",
        }:
            remote_key_operation(bootstrap, store, arguments)
            return
        if arguments.command == "config":
            document = store.public()
            if arguments.operation == "show":
                print(json.dumps(document, indent=2))
                return
            configuration = document["configuration"]
            if arguments.operation == "apply":
                configuration = json.loads(arguments.file.read_text(encoding="utf-8"))
            else:
                parts = arguments.path.split(".")
                target = configuration
                for name in parts[:-1]:
                    target = target[name]
                target[parts[-1]] = json.loads(arguments.value)
            revision = management.configure(
                ConfigurationUpdate(revision=document["revision"], configuration=configuration)
            )
            print(
                f"Saved configuration revision {revision}. Running servers activate it automatically."
            )
            return
        if arguments.command in {"key", "token", "service-key"}:
            revision = store.load()[0]
            value = (
                None
                if arguments.operation in {"remove", "rename"}
                else prompt_secret("New credential: ")
            )
            update = CredentialUpdate(
                revision=revision,
                operation=arguments.operation,
                provider=arguments.name if arguments.command in {"key", "service-key"} else None,
                field="service_key" if arguments.command == "service-key" else None,
                token_name=arguments.name if arguments.command == "token" else None,
                index=getattr(arguments, "index", None),
                name=getattr(arguments, "key_name", None),
                value=value,
            )
            revision = management.credential(update)
            print(f"Saved credential update at revision {revision}.")
            return
        app = create_app(store=store)
    except Exception:
        print(
            "Operation failed. Check initialization, protected-key access, settings and revision. See docs/configuration.md.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    uvicorn.run(app, host=bootstrap.host, port=bootstrap.port, workers=1, access_log=False)
