import os
import sqlite3

import pytest

from search_router.config import Settings
from search_router.protected import ProtectionError, SecretCipher
from search_router.storage import ConfigurationError, ConfigurationStore


def store_with_settings(tmp_path):
    store = ConfigurationStore(tmp_path / "config.sqlite3", SecretCipher(os.urandom(32)))
    store.initialize(
        Settings(
            tokens={"admin": "a-protected-admin-token-at-least-24"},
            providers={"tavily": {"keys": ["private-provider-key"]}},
        )
    )
    return store


def test_exa_service_key_is_protected_and_usage_ids_follow_key_lifecycle(tmp_path):
    from search_router.management import CredentialUpdate, Management

    store = store_with_settings(tmp_path)
    management = Management(store)
    management.credential(
        CredentialUpdate(revision=1, operation="add", provider="exa", value="search-key-one")
    )
    management.credential(
        CredentialUpdate(revision=2, operation="add", provider="exa", value="search-key-two")
    )
    management.credential(
        CredentialUpdate(
            revision=3,
            operation="add",
            provider="exa",
            field="service_key",
            value="private-service-key",
        )
    )
    assert b"private-service-key" not in store.path.read_bytes()
    public = store.public()
    assert "private-service-key" not in str(public)
    assert public["provider_credentials"]["exa"]["service_key"] is True
    assert "service_key" not in public["configuration"]["providers"]["exa"]
    public["configuration"]["providers"]["exa"]["usage_key_ids"] = ["first-id", "second-id"]
    store.save(store.candidate(public["configuration"], store.load()[1]), 4)
    assert store.load()[1].providers["exa"].service_key.get_secret_value() == "private-service-key"
    management.credential(CredentialUpdate(revision=5, operation="remove", provider="exa", index=1))
    assert store.load()[1].providers["exa"].usage_key_ids == ["second-id"]
    management.credential(
        CredentialUpdate(
            revision=6, operation="replace", provider="exa", index=1, value="new-search-key"
        )
    )
    assert store.load()[1].providers["exa"].usage_key_ids == [""]
    management.credential(
        CredentialUpdate(revision=7, operation="remove", provider="exa", field="service_key")
    )
    assert store.public()["provider_credentials"]["exa"]["service_key"] is False


def test_cli_exa_service_key_uses_protected_prompt(tmp_path, monkeypatch, capsys):
    from search_router import cli

    store = store_with_settings(tmp_path)
    monkeypatch.setattr(cli, "open_store", lambda bootstrap, create=False: store)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "private-service-key")
    monkeypatch.setattr("sys.argv", ["search-router", "service-key", "add", "exa"])
    cli.main()
    assert store.load()[1].providers["exa"].service_key.get_secret_value() == "private-service-key"
    assert "private-service-key" not in capsys.readouterr().out


async def test_exa_quota_refresh_uses_shared_admin_and_public_contracts(tmp_path):
    import httpx

    from search_router.api.app import create_app
    from search_router.providers.exa import ExaProvider
    from search_router.providers.keys import KeyPool

    store = store_with_settings(tmp_path)
    revision, settings = store.load()
    settings = Settings(
        tokens=settings.tokens,
        providers={
            "exa": {
                "keys": ["private-search-key"],
                "service_key": "private-service-key",
                "usage_key_ids": ["fixture"],
            }
        },
    )
    store.save(settings, revision)
    probes = []

    def upstream(request):
        probes.append(request)
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "total_cost_usd": 3.25,
                "period": {
                    "start": request.url.params["start_date"],
                    "end": request.url.params["end_date"],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        pool = KeyPool("exa", settings, [ExaProvider(settings.providers["exa"], upstream_client)])
        app = create_app(store=store, providers=[pool])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://testserver",
                headers={"Authorization": "Bearer a-protected-admin-token-at-least-24"},
            ) as client:
                result = (
                    await client.post(
                        "/admin/api/key-actions",
                        json={
                            "revision": 2,
                            "provider": "exa",
                            "index": 1,
                            "action": "refresh_quota",
                            "confirm_live": True,
                        },
                    )
                ).json()
                assert result["status"] == "refreshed"
                assert result["quota"]["remaining"] == 6.75
                assert result["quota_probe_attempts"] == 1
                assert app.state.gateway.quotas("exa").keys[0].quota.unit == "usd"
                assert app.state.gateway.provider("exa").key_pool.keys[0].quota.remaining == 6.75
                document = (await client.get("/admin/api/configuration")).json()
                assert document["key_status"]["exa"][0]["quota"]["budget_usd"] == 10
                assert "private-service-key" not in str(document)
                assert len(probes) == app.state.quotas.probe_attempts == 1
                assert app.state.router.budget.attempts == 0


def test_store_encrypts_all_credentials_and_exposes_only_metadata(tmp_path):
    store = store_with_settings(tmp_path)
    contents = store.path.read_bytes()
    assert b"private-provider-key" not in contents
    assert b"a-protected-admin-token" not in contents
    public = store.public()
    assert public["credentials"]["tavily"] == 1
    assert public["tokens"] == ["admin"]
    assert "private-provider-key" not in str(public)
    assert "a-protected-admin-token" not in str(public)
    revision, settings = store.load()
    assert (
        settings.providers["tavily"].credentials()[0].get_secret_value() == "private-provider-key"
    )
    assert revision == 1


def test_masked_configuration_edits_preserve_keys_and_reject_stale_writes(tmp_path):
    store = store_with_settings(tmp_path)
    revision, settings = store.load()
    configuration = store.public()["configuration"]
    configuration["providers"]["tavily"]["depth"] = "advanced"
    candidate = store.candidate(configuration, settings)
    assert store.save(candidate, revision) == 2
    assert (
        store.load()[1].providers["tavily"].credentials()
        == settings.providers["tavily"].credentials()
    )
    with pytest.raises(ConfigurationError, match="configuration_conflict"):
        store.save(settings, revision)


def test_cipher_rejects_wrong_key_and_credential_substitution(tmp_path):
    cipher = SecretCipher(os.urandom(32))
    sealed = cipher.encrypt("first", "secret")
    with pytest.raises(ProtectionError):
        cipher.decrypt("second", sealed)
    with pytest.raises(ProtectionError):
        SecretCipher(os.urandom(32)).decrypt("first", sealed)
    store = store_with_settings(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE credentials SET sealed=?", (b"invalid",))
    with pytest.raises(ConfigurationError, match="invalid_or_locked"):
        store.load()


def test_bootstrap_reads_only_process_bootstrap_values(tmp_path, monkeypatch):
    from search_router.config import BootstrapSettings

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ROUTER_PORT=1234", encoding="utf-8")
    monkeypatch.setenv("ROUTER_DATABASE", str(tmp_path / "private.sqlite3"))
    monkeypatch.setenv("ROUTER_PORT", "9094")
    monkeypatch.setenv("ROUTER_TOKENS", '{"unexpected":"private-old-env-token"}')
    bootstrap = BootstrapSettings()
    assert bootstrap.port == 9094
    assert bootstrap.database == tmp_path / "private.sqlite3"
    assert Settings().tokens == {}
    monkeypatch.delenv("ROUTER_PORT")
    assert BootstrapSettings().port == 9092


def test_external_master_key_and_native_vault_fail_closed(tmp_path, monkeypatch):
    import base64

    import keyring

    from search_router.protected import system_cipher

    key_file = tmp_path / "external.key"
    key_file.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    cipher = system_cipher(tmp_path / "configuration.sqlite3", key_file)
    assert cipher.decrypt("record", cipher.encrypt("record", "fixture")) == "fixture"
    key_file.write_bytes(b"invalid-key")
    with pytest.raises(ProtectionError):
        system_cipher(tmp_path / "configuration.sqlite3", key_file, create=True)
    insecure_backend = type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file"})()
    monkeypatch.setattr(keyring, "get_keyring", lambda: insecure_backend)
    with pytest.raises(ProtectionError, match="os_credential_store_unavailable"):
        system_cipher(tmp_path / "configuration.sqlite3", create=True)


def test_additional_provider_secrets_are_encrypted_and_preserved(tmp_path):
    from test_registry import example_registry

    registry = example_registry()
    store = ConfigurationStore(
        tmp_path / "configuration.sqlite3", SecretCipher(os.urandom(32)), registry
    )
    settings = Settings(
        registry=registry,
        tokens={"admin": "a-protected-admin-token-at-least-24"},
        providers={"example": {"token": "private-additional-provider-secret"}},
    )
    store.initialize(settings)
    document = store.public()
    assert "private-additional-provider-secret" not in str(document)
    assert b"private-additional-provider-secret" not in store.path.read_bytes()
    candidate = store.candidate(document["configuration"], settings)
    store.save(candidate, 1)
    assert (
        store.load()[1].providers["example"].token.get_secret_value()
        == "private-additional-provider-secret"
    )


def test_management_key_lifecycle_and_last_token_protection(tmp_path):
    from search_router.management import CredentialUpdate, Management

    store = store_with_settings(tmp_path)
    management = Management(store)
    assert (
        management.credential(
            CredentialUpdate(
                revision=1, operation="add", provider="tavily", value="second-private-key"
            )
        )
        == 2
    )
    assert (
        management.credential(
            CredentialUpdate(
                revision=2, operation="replace", provider="tavily", index=1, value="replacement-key"
            )
        )
        == 3
    )
    assert (
        management.credential(
            CredentialUpdate(revision=3, operation="remove", provider="tavily", index=2)
        )
        == 4
    )
    with pytest.raises(ConfigurationError, match="valid_router_token_required"):
        management.credential(CredentialUpdate(revision=4, operation="remove", token_name="admin"))
    assert store.load()[0] == 4


async def test_management_api_auth_validation_conflicts_and_reload(tmp_path):
    import httpx

    from search_router.api.app import create_app

    store = store_with_settings(tmp_path)
    app = create_app(store=store, providers=[])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/admin/api/configuration")).status_code == 401
            client.headers["Authorization"] = "Bearer a-protected-admin-token-at-least-24"
            document = (await client.get("/admin/api/configuration")).json()
            document["configuration"]["hourly_attempt_limit"] = 23
            app.state.router.budget.attempts = 7
            response = await client.put(
                "/admin/api/configuration",
                json={"revision": 1, "configuration": document["configuration"]},
            )
            assert response.status_code == 200
            assert response.json()["active_revision"] == 2
            assert app.state.gateway.runtime.router.budget.attempts == 7
            assert app.state.gateway.runtime.settings.hourly_attempt_limit == 23
            assert (
                await client.put(
                    "/admin/api/configuration",
                    json={"revision": 1, "configuration": document["configuration"]},
                )
            ).status_code == 409
            assert (
                await client.post(
                    "/admin/api/credentials",
                    json={
                        "revision": 2,
                        "operation": "replace",
                        "token_name": "admin",
                        "value": "new-protected-admin-token-at-least-24",
                    },
                )
            ).status_code == 200
            assert (await client.get("/admin/api/configuration")).status_code == 401
            client.headers["Authorization"] = "Bearer new-protected-admin-token-at-least-24"
            assert (await client.get("/admin/api/configuration")).status_code == 200


def test_cli_uses_shared_management_without_printing_secrets(tmp_path, monkeypatch, capsys):
    from search_router import cli

    store = store_with_settings(tmp_path)
    monkeypatch.setattr(cli, "open_store", lambda bootstrap, create=False: store)
    monkeypatch.setattr(
        "sys.argv", ["search-router", "config", "set", "providers.tavily.depth", '"advanced"']
    )
    cli.main()
    assert store.load()[1].providers["tavily"].depth == "advanced"
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "another-private-key")
    monkeypatch.setattr("sys.argv", ["search-router", "key", "add", "tavily"])
    cli.main()
    assert len(store.load()[1].providers["tavily"].credentials()) == 2
    assert "private" not in capsys.readouterr().out


def test_cli_initializes_once_without_echoing_the_administrator_token(
    tmp_path, monkeypatch, capsys
):
    from search_router import cli

    store = ConfigurationStore(tmp_path / "new.sqlite3", SecretCipher(os.urandom(32)))
    monkeypatch.setattr(cli, "open_store", lambda bootstrap, create=False: store)
    token = "private-administrator-token-for-initialization"
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: token)
    monkeypatch.setattr("sys.argv", ["search-router", "--database", str(store.path), "init"])
    cli.main()
    assert store.load()[1].tokens["admin"].get_secret_value() == token
    assert token not in capsys.readouterr().out
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 2
    assert store.load()[0] == 1


def test_cli_rejects_a_concurrent_change_during_a_secret_prompt(tmp_path, monkeypatch):
    from search_router import cli
    from search_router.management import ConfigurationUpdate, Management

    store = store_with_settings(tmp_path)
    monkeypatch.setattr(cli, "open_store", lambda bootstrap, create=False: store)

    def concurrent_update(prompt):
        document = store.public()
        document["configuration"]["hourly_attempt_limit"] = 31
        Management(store).configure(
            ConfigurationUpdate(revision=1, configuration=document["configuration"])
        )
        return "private-concurrent-prompt-key"

    monkeypatch.setattr(cli.getpass, "getpass", concurrent_update)
    monkeypatch.setattr("sys.argv", ["search-router", "key", "replace", "tavily", "1"])
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 2
    revision, settings = store.load()
    assert revision == 2
    assert (
        settings.providers["tavily"].credentials()[0].get_secret_value() == "private-provider-key"
    )


async def test_external_cli_revisions_activate_and_startup_changes_are_reported(tmp_path):
    from search_router.api.app import create_app
    from search_router.management import ConfigurationUpdate, Management

    store = store_with_settings(tmp_path)
    app = create_app(store=store, providers=[])
    async with app.router.lifespan_context(app):
        document = store.public()
        document["configuration"]["allowed_hosts"].append("router.example")
        document["configuration"]["hourly_attempt_limit"] = 37
        Management(store).configure(
            ConfigurationUpdate(revision=1, configuration=document["configuration"])
        )
        controller = app.state.configuration
        async with controller.lock:
            await controller.activate()
        status = controller.status()
        assert status["active_revision"] == status["revision"] == 2
        assert status["restart_required"] == ["allowed_hosts"]
        assert app.state.gateway.runtime.settings.hourly_attempt_limit == 37


async def test_client_tokens_cannot_manage_configuration_and_validation_is_safe(tmp_path):
    import httpx

    from search_router.api.app import create_app
    from search_router.management import CredentialUpdate, Management

    store = store_with_settings(tmp_path)
    Management(store).credential(
        CredentialUpdate(
            revision=1,
            operation="add",
            token_name="client",
            value="ordinary-client-token-at-least-24",
        )
    )
    app = create_app(store=store, providers=[])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/admin")).status_code == 200
            assert (await client.get("/admin/app.js")).status_code == 200
            assert (await client.get("/admin/icons.svg")).status_code == 200
            assert (await client.get("/admin/public-sans.woff2")).status_code == 200
            client.headers["Authorization"] = "Bearer ordinary-client-token-at-least-24"
            assert (await client.get("/capabilities")).status_code == 200
            assert (await client.get("/admin/api/configuration")).status_code == 403
            client.headers["Authorization"] = "Bearer a-protected-admin-token-at-least-24"
            configuration = store.public()["configuration"]
            configuration["providers"]["tavily"]["depth"] = "private-invalid-input"
            response = await client.put(
                "/admin/api/configuration", json={"revision": 2, "configuration": configuration}
            )
            assert response.status_code == 422
            assert "private-invalid-input" not in response.text
            assert store.load()[0] == 2
            response = await client.post(
                "/admin/api/credentials",
                json={"revision": 2, "operation": "remove", "token_name": "admin"},
            )
            assert response.status_code == 422
            assert store.load()[0] == 2
            client.headers["Origin"] = "https://other.example"
            assert (await client.get("/admin/api/configuration")).status_code == 403


async def test_reload_drains_inflight_search_and_preserves_accounting():
    import asyncio

    from test_service import FakeProvider

    from search_router.application import Gateway
    from search_router.contracts import SearchRequest
    from search_router.runtime import Runtime

    release = asyncio.Event()
    provider = FakeProvider("searxng", wait=release)
    original = Runtime(Settings(provider_order=["searxng"]), providers=[provider])
    await original.start()
    gateway = Gateway(original)
    request = asyncio.create_task(gateway.search(SearchRequest(query="fixture"), "test"))
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    replacement = Settings(provider_order=[], hourly_attempt_limit=13)
    reload = asyncio.create_task(gateway.reconfigure(replacement))
    await asyncio.sleep(0)
    assert not reload.done()
    assert gateway.runtime is original
    release.set()
    try:
        response, _ = await asyncio.gather(request, reload)
        assert response.status == "empty"
        assert gateway.runtime.settings.hourly_attempt_limit == 13
        assert gateway.runtime.router.budget.attempts == 1
        assert not original.started
    finally:
        await gateway.runtime.close()


async def test_reload_preserves_key_cooldowns_health_and_quota_probe_ceiling(tmp_path):
    from test_registry import example_registry

    from search_router.application import Gateway
    from search_router.runtime import Runtime

    settings = Settings(
        registry=example_registry(),
        quota_cache_enabled=True,
        quota_cache_dir=str(tmp_path / "original-cache"),
        providers={"example": {"keys": ["example-key-one", "example-key-two"]}},
    )
    runtime = Runtime(settings)
    await runtime.start()
    gateway = Gateway(runtime)
    pool = runtime.router.providers["example"]
    pool.states[0].calls = 8
    pool.states[0].blocked_until = pool.clock() + 300
    pool.blocked_until = pool.clock() + 60
    runtime.router.health["example"].failures = 2
    runtime.quotas.probes.append(runtime.quotas.wall_clock())
    runtime.quotas.probe_attempts = 4
    try:
        await gateway.reconfigure(
            settings.model_copy(
                update={"cache_ttl": 9, "quota_cache_dir": str(tmp_path / "new-cache")}
            )
        )
        new_pool = gateway.runtime.router.providers["example"]
        assert new_pool.states[0].calls == 8
        assert new_pool.states[0].blocked_until == pool.states[0].blocked_until
        assert new_pool.blocked_until == pool.blocked_until
        assert gateway.runtime.router.health["example"].failures == 2
        assert len(gateway.runtime.quotas.probes) == 1
        assert gateway.runtime.quotas.probe_attempts == 4
        assert gateway.runtime.quotas.path.is_file()
        assert gateway.runtime.quotas.storage_status == "ready"
    finally:
        await gateway.runtime.close()


def test_secret_prompt_refuses_echo_fallback(monkeypatch):
    import warnings

    from search_router import cli

    def fallback(prompt):
        warnings.warn("echo fallback", cli.getpass.GetPassWarning)
        pytest.fail("An insecure prompt must not continue")

    monkeypatch.setattr(cli.getpass, "getpass", fallback)
    with pytest.raises(cli.getpass.GetPassWarning):
        cli.prompt_secret("Fixture credential: ")


def test_named_keys_and_bounded_previews_stay_aligned(tmp_path):
    from search_router.management import ConfigurationUpdate, CredentialUpdate, Management

    store = store_with_settings(tmp_path)
    management = Management(store)
    secret = "long-test-key-with-hidden-characters-1234"
    management.credential(
        CredentialUpdate(
            revision=1, operation="add", provider="tavily", name="Research", value=secret
        )
    )
    metadata = store.public()["key_metadata"]["tavily"]
    assert metadata[1] == {"index": 2, "name": "Research", "preview": "long...1234"}
    assert secret not in str(store.public())
    management.credential(
        CredentialUpdate(
            revision=2, operation="rename", provider="tavily", index=2, name="Production"
        )
    )
    assert store.load()[1].providers["tavily"].credentials()[1].get_secret_value() == secret
    management.credential(
        CredentialUpdate(
            revision=3, operation="replace", provider="tavily", index=2, value="short-key"
        )
    )
    assert store.public()["key_metadata"]["tavily"][1] == {
        "index": 2,
        "name": "Production",
        "preview": "********",
    }
    management.credential(
        CredentialUpdate(revision=4, operation="remove", provider="tavily", index=1)
    )
    assert store.public()["key_metadata"]["tavily"] == [
        {"index": 1, "name": "Production", "preview": "********"}
    ]
    document = store.public()
    management.configure(ConfigurationUpdate(revision=5, configuration=document["configuration"]))
    assert store.public()["key_metadata"]["tavily"][0]["name"] == "Production"


def test_cli_can_rename_a_key_without_a_secret_prompt(tmp_path, monkeypatch):
    from search_router import cli

    store = store_with_settings(tmp_path)
    original = store.load()[1].providers["tavily"].credentials()
    monkeypatch.setattr(cli, "open_store", lambda bootstrap, create=False: store)
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt: pytest.fail("Rename must not request a secret")
    )
    monkeypatch.setattr(
        "sys.argv", ["search-router", "key", "rename", "tavily", "1", "--name", "Production"]
    )
    cli.main()
    assert store.public()["key_metadata"]["tavily"][0]["name"] == "Production"
    assert store.load()[1].providers["tavily"].credentials() == original


async def test_key_actions_require_opt_in_and_share_search_and_quota_budgets(tmp_path):
    import httpx
    from test_registry import example_registry

    from search_router.api.app import create_app

    registry = example_registry()
    store = ConfigurationStore(tmp_path / "actions.sqlite3", SecretCipher(os.urandom(32)), registry)
    store.initialize(
        Settings(
            registry=registry,
            tokens={"admin": "administrator-token-at-least-24"},
            providers={
                "example": {
                    "keys": ["private-key-one", "private-key-two"],
                    "estimated_cost_usd": 0.01,
                }
            },
            quota_refresh_hourly_limit=1,
            hourly_attempt_limit=1,
        )
    )
    app = create_app(store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer administrator-token-at-least-24"},
        ) as client:
            action = {
                "revision": 1,
                "provider": "example",
                "index": 2,
                "action": "test",
                "max_cost_usd": 0.01,
            }
            assert (
                await client.post(
                    "/admin/api/key-actions",
                    json=action,
                    headers={"Authorization": "Bearer invalid"},
                )
            ).status_code == 401
            assert (
                await client.get(
                    "/admin/api/keys/example", headers={"Authorization": "Bearer invalid"}
                )
            ).status_code == 401
            assert (await client.post("/admin/api/key-actions", json=action)).status_code == 422
            assert app.state.router.budget.attempts == 0
            action["confirm_live"] = True
            action["max_cost_usd"] = 0
            assert (await client.post("/admin/api/key-actions", json=action)).json()[
                "reason"
            ] == "test_cost_ceiling_exceeded"
            action["max_cost_usd"] = 0.01
            result = (await client.post("/admin/api/key-actions", json=action)).json()
            assert result["status"] == "working"
            pool = app.state.router.providers["example"]
            assert [child.calls for child in pool.providers] == [0, 1]
            assert app.state.router.budget.attempts == 1
            assert (await client.post("/admin/api/key-actions", json=action)).json()[
                "reason"
            ] == "attempt_budget_exhausted"
            action["action"] = "refresh_quota"
            result = (await client.post("/admin/api/key-actions", json=action)).json()
            assert result["status"] == "refreshed"
            assert result["quota"]["remaining"] == 12
            assert (await client.post("/admin/api/key-actions", json=action)).json()[
                "reason"
            ] == "quota_probe_budget_exhausted"
            before = app.state.quotas.probe_attempts
            status = (await client.get("/admin/api/keys/example")).json()
            assert status["status"][1]["quota"]["remaining"] == 12
            assert app.state.quotas.probe_attempts == before == 1
            assert "private-key" not in str(status)
            action["revision"] = 2
            assert (await client.post("/admin/api/key-actions", json=action)).status_code == 409


def test_cli_key_diagnostics_use_authenticated_running_server(tmp_path, monkeypatch, capsys):
    import httpx

    from search_router import cli

    store = store_with_settings(tmp_path)
    monkeypatch.setattr(cli, "open_store", lambda bootstrap, create=False: store)
    original_client = httpx.Client
    requests = []

    def response(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer a-protected-admin-token-at-least-24"
        return httpx.Response(200, json={"status": "working"})

    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["search-router", "key", "test", "tavily", "1", "--confirm-live", "--max-cost-usd", "0.02"],
    )
    cli.main()
    assert len(requests) == 1
    assert requests[0].url.path == "/admin/api/key-actions"
    assert b'"max_cost_usd":0.02' in requests[0].content
    assert "protected-admin" not in capsys.readouterr().out


async def test_key_check_failures_unknown_cost_and_unsupported_quota_are_explicit(tmp_path):
    from test_service import FakeProvider

    from search_router.key_checks import KeyAction, KeyChecks
    from search_router.providers.base import ProviderError
    from search_router.providers.keys import KeyPool
    from search_router.runtime import Runtime

    settings = Settings(providers={"tavily": {"keys": ["private-fixture-key"]}})
    child = FakeProvider("tavily", error=ProviderError("authentication"))
    pool = KeyPool("tavily", settings, [child])
    runtime = Runtime(settings, providers=[pool])
    await runtime.start()
    try:
        checks = KeyChecks(runtime)
        action = KeyAction(
            revision=1,
            provider="tavily",
            index=1,
            action="test",
            confirm_live=True,
            max_cost_usd=0.01,
        )
        assert (await checks.run(action)).reason == "unknown_cost_estimate"
        assert child.calls == runtime.router.budget.attempts == 0
        assert (
            await checks.run(action.model_copy(update={"action": "refresh_quota"}))
        ).status == "unsupported"
        assert runtime.quotas.probe_attempts == 0
        settings.providers["tavily"].estimated_cost_usd = 0.01
        failure = await checks.run(action)
        assert failure.reason == "authentication"
        assert failure.status == "failed"
        assert failure.usage.upstream_attempts == 1
        skipped = await checks.run(action)
        assert skipped.status == "skipped"
        assert child.calls == runtime.router.budget.attempts == 1
        assert "private-fixture-key" not in str(pool.states[0].last_test)
    finally:
        await runtime.close()


async def test_key_check_lease_delays_reload_until_the_test_finishes(tmp_path):
    import asyncio

    import httpx
    from test_registry import example_registry

    from search_router.api.app import create_app

    registry = example_registry()
    store = ConfigurationStore(tmp_path / "leased.sqlite3", SecretCipher(os.urandom(32)), registry)
    store.initialize(
        Settings(
            registry=registry,
            tokens={"admin": "administrator-token-at-least-24"},
            providers={"example": {"keys": ["private-fixture-key"], "estimated_cost_usd": 0.01}},
        )
    )
    app = create_app(store=store)
    async with app.router.lifespan_context(app):
        child = app.state.router.providers["example"].providers[0]
        child.wait = asyncio.Event()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer administrator-token-at-least-24"},
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/admin/api/key-actions",
                    json={
                        "revision": 1,
                        "provider": "example",
                        "index": 1,
                        "action": "test",
                        "confirm_live": True,
                        "max_cost_usd": 0.01,
                    },
                )
            )
            await asyncio.wait_for(child.entered.wait(), 1)
            assert app.state.gateway.active_operations == 1
            configuration = store.public()["configuration"]
            configuration["attempt_timeout"] = 9
            update = asyncio.create_task(
                client.put(
                    "/admin/api/configuration", json={"revision": 1, "configuration": configuration}
                )
            )
            await asyncio.sleep(0)
            assert not update.done()
            child.wait.set()
            result, saved = await asyncio.gather(task, update)
            assert result.json()["status"] == "working"
            assert saved.json()["active_revision"] == 2
            assert app.state.router.budget.attempts == 1
            assert app.state.router.providers["example"].states[0].last_test["status"] == "working"
