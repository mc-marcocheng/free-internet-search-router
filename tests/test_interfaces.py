import asyncio
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_policy import result
from test_service import FakeProvider

from search_router.api.app import create_app
from search_router.config import Settings
from search_router.contracts import SearchResponse

TOKEN = "test-only-router-token-at-least-24"


async def test_auth_validation_schemas_and_health():
    app = create_app(Settings(tokens={"test": TOKEN}), providers=[FakeProvider("exa", [result()])])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 200
            assert (await client.get("/providers")).status_code == 401
            assert (await client.post("/mcp", json={})).status_code == 401
            client.headers["Authorization"] = "Bearer " + TOKEN
            assert (await client.get("/schemas/SearchResponse")).status_code == 200
            assert (await client.get("/openapi.json")).status_code == 200
            assert app.openapi()["info"]["title"] == "Free Internet Search Router"
            failure = await client.post("/search", json={"query": "", "password": "sensitive"})
            assert failure.status_code == 422
            assert "sensitive" not in failure.text
            assert (
                await client.get("/providers", headers={"Origin": "https://evil.org"})
            ).status_code == 403


async def test_rest_mcp_wire_parity_and_concurrent_calls():
    provider = FakeProvider("exa", [result()])
    app = create_app(Settings(tokens={"test": TOKEN}), providers=[provider])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + TOKEN},
        ) as client:
            rest = (await client.post("/search", json={"query": "market"})).json()
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                reader,
                writer,
                _,
            ):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "Free Internet Search Router"
                    tools = (await session.list_tools()).tools
                    assert {tool.name for tool in tools} == {
                        "search_web",
                        "read_url",
                        "get_capabilities",
                        "get_providers",
                        "get_provider",
                        "discover_provider",
                        "get_provider_quotas",
                    }
                    assert next(tool for tool in tools if tool.name == "search_web").outputSchema
                    responses = await asyncio.gather(
                        *(
                            session.call_tool("search_web", {"request": {"query": "market"}})
                            for index in range(3)
                        )
                    )
                    for response in responses:
                        assert not response.isError
                        data = SearchResponse.model_validate(response.structuredContent)
                        assert data.status == rest["status"]
                        assert data.results[0].model_dump(mode="json") == rest["results"][0]
                        assert data.cache.hit
                        assert json.loads(response.content[0].text) == response.structuredContent
                    caps = await session.call_tool("get_capabilities", {})
                    assert caps.structuredContent["schema_version"] == "1.0"
            assert provider.calls == 1


def test_missing_router_auth_refuses_startup():
    with pytest.raises(RuntimeError, match="search-router init"):
        create_app(Settings())


async def test_chunked_body_limit():
    app = create_app(Settings(tokens={"test": TOKEN}), providers=[])

    async def body():
        yield b"a" * 40000
        yield b"b" * 40000

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + TOKEN},
        ) as client:
            response = await client.post("/search", content=body())
            assert response.status_code == 413


@pytest.mark.parametrize("minimal", [None, True, False])
async def test_read_rest_mcp_parity(minimal):
    from search_router.contracts import ReadResponse

    document = ReadResponse(
        status="ok",
        original_url="https://example.org",
        final_url="https://example.org/",
        text="Exact\nsource  text",
        content_type="text/plain",
        warnings=[],
    )

    class FakeReader:
        async def read(self, request):
            assert request.url == document.original_url
            assert request.minimal is (True if minimal is None else minimal)
            return document.model_copy(deep=True)

    payload = {"url": document.original_url}
    if minimal is not None:
        payload["minimal"] = minimal
    app = create_app(Settings(tokens={"test": TOKEN}), providers=[], reader=FakeReader())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + TOKEN},
        ) as client:
            rest = await client.post("/read", json=payload)
            assert rest.status_code == 200
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                reader,
                writer,
                _,
            ):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    result = await session.call_tool("read_url", {"request": payload})
                    assert not result.isError
                    assert result.structuredContent == rest.json()


async def test_runtime_closes_owned_resources_on_partial_startup_failure(monkeypatch):
    from search_router.providers.quotas import QuotaTracker
    from search_router.runtime import Runtime

    class CloseableProvider(FakeProvider):
        closed = 0

        async def aclose(self):
            self.closed += 1

    async def fail_start(self):
        raise RuntimeError("controlled_start_failure")

    monkeypatch.setattr(QuotaTracker, "start", fail_start)
    provider = CloseableProvider("exa")
    runtime = Runtime(Settings(), [provider])
    with pytest.raises(RuntimeError, match="controlled_start_failure"):
        await runtime.start()
    assert not runtime.started
    assert provider.closed == 1
    await runtime.close()
    assert provider.closed == 1


async def test_runtime_closes_pooled_children_once_after_draining_work():
    from search_router.providers.keys import KeyPool
    from search_router.runtime import Runtime

    class CloseableProvider(FakeProvider):
        closed = 0

        async def aclose(self):
            assert self.cancelled.is_set()
            self.closed += 1

    settings = Settings()
    provider = CloseableProvider("exa", wait=asyncio.Event())
    pool = KeyPool("exa", settings, [provider])
    runtime = Runtime(settings, [pool])
    await runtime.start()
    from search_router.contracts import SearchRequest

    task = asyncio.create_task(runtime.router.search(SearchRequest(query="market"), "test"))
    await provider.entered.wait()
    await runtime.close()
    await asyncio.gather(task, return_exceptions=True)
    assert provider.closed == 1
    assert runtime.quotas.task.done()
