import asyncio
import os
import socket

import httpx
import pytest

from search_router.config import Settings
from search_router.contracts import ReadRequest
from search_router.reader import Reader
from search_router.reader.security import ReadFailure, resolve_public, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@example.org",
        "http://example.org:9091",
        "http://example.org/a?api_key=secret",
        "http://example.org\\@localhost",
        "http://[fe80::1%25eth0]",
    ],
)
def test_unsafe_urls(url):
    with pytest.raises(ReadFailure):
        validate_url(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1"]
)
async def test_non_public_dns(monkeypatch, address):
    async def lookup(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(ReadFailure):
        await resolve_public("example.org", 80)


async def test_redirect_rechecked_and_connection_pinned():
    seen = []

    async def resolver(host, port):
        seen.append(host)
        if host == "localhost":
            raise ReadFailure("non_public_address", blocked=True)
        return ["93.184.216.34"]

    def upstream(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        return httpx.Response(302, headers={"location": "http://localhost/private"})

    reader = Reader(
        Settings(),
        resolver=resolver,
        client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(upstream), **kwargs
        ),
    )
    response = await reader.read(ReadRequest(url="https://example.org"))
    assert response.status == "blocked"
    assert seen == ["example.org", "localhost"]


async def test_download_limit():
    async def resolver(host, port):
        return ["93.184.216.34"]

    reader = Reader(
        Settings(reader_limit_bytes=1024),
        resolver=resolver,
        client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers={"content-length": "10000"})
            ),
            **kwargs,
        ),
    )
    response = await reader.read(ReadRequest(url="https://example.org"))
    assert "download_limit" in response.warnings


async def test_real_extraction_worker_preserves_text_and_truncates():
    reader = Reader(Settings())
    response = await reader.extract(b"Exact\ntext  with spaces", "text/plain", 10)
    assert response["text"] == "Exact\ntext"
    assert response["truncated"]
    response = await reader.extract(
        b"<html><body><main><p>Original <b>words</b>.</p></main>"
        b"<script>not content</script></body></html>",
        "text/html",
        1000,
    )
    assert "Original " in response["text"]
    assert "not content" not in response["text"]


@pytest.mark.parametrize("max_chars", [20, 1000])
async def test_reader_standard_conversion_recovers_empty_minimal_without_browser(
    monkeypatch, max_chars
):
    reader = Reader(Settings())
    html = (
        b"<html><body><p>Financial statement</p>"
        b"<table><tr><td>Revenue</td><td>100</td></tr></table></body></html>"
    )

    async def download(url):
        return html, "text/html", url

    async def browser(url):
        pytest.fail("conversion recovery must not perform another browser download")

    monkeypatch.setattr(reader, "download", download)
    monkeypatch.setattr(reader, "browser", browser)
    response = await reader.read(ReadRequest(url="https://example.org/filing", max_chars=max_chars))
    assert response.status == "ok"
    assert response.text.startswith("Financial statement")
    assert "html_to_markdown_mdream_standard_fallback" in response.warnings
    assert "reader_backend:httpx" in response.warnings
    assert "html_to_markdown_mdream_minimal" not in response.warnings
    assert response.truncated == (max_chars == 20)
    assert len(response.text) <= max_chars


@pytest.mark.parametrize(
    "html",
    [
        b"<html><body><main>Verify you are human</main></body></html>",
        b"<html><body><p>Verify you are human</p></body></html>",
    ],
)
async def test_challenge_pages_not_documents(html):
    with pytest.raises(ReadFailure, match="challenge_page"):
        await Reader(Settings()).extract(html, "text/html", 1000)


@pytest.mark.parametrize("minimal", [True, False])
@pytest.mark.parametrize("backend", ["httpx", "cloakbrowser"])
async def test_reader_uses_requested_conversion_mode(monkeypatch, minimal, backend):
    reader = Reader(Settings(reader_backends=[backend]))

    async def download(url):
        return (
            b"<main><p>Article body.</p></main><aside>Additional financial details.</aside>",
            "text/html",
            url,
        )

    monkeypatch.setattr(reader, "download", download)
    monkeypatch.setattr(reader, "browser", download)
    response = await reader.read(ReadRequest(url="https://example.org/filing", minimal=minimal))
    assert response.status == "ok"
    assert "Article body" in response.text
    assert ("Additional financial details" in response.text) == (not minimal)
    expected_mode = "minimal" if minimal else "standard"
    assert "html_to_markdown_mdream_" + expected_mode in response.warnings
    assert "reader_backend:" + backend in response.warnings
    assert "html_to_markdown_mdream_standard_fallback" not in response.warnings


@pytest.mark.parametrize("minimal", [True, False])
async def test_conversion_mode_does_not_change_plain_text(minimal):
    response = await Reader(Settings()).extract(
        b"Exact\nsource  text", "text/plain", 1000, minimal=minimal
    )
    assert response["text"] == "Exact\nsource  text"
    assert response["warnings"] == []


@pytest.mark.parametrize("minimal", [True, False])
async def test_pdf_worker_extracts_document_text(minimal):
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Exact PDF source text) Tj ET")
    page[NameObject("/Contents")] = stream
    buffer = io.BytesIO()
    writer.write(buffer)
    document = await Reader(Settings()).extract(
        buffer.getvalue(), "application/pdf", 1000, minimal=minimal
    )
    assert document["text"] == "Exact PDF source text"
    assert not document["truncated"]
    assert "pdf_text_only_no_ocr_layout_may_differ" in document["warnings"]


async def test_browser_proxy_rejects_private_destinations_before_connect(monkeypatch):
    from search_router.reader.browser_proxy import PublicBrowserProxy

    async def resolver(host, port):
        raise ReadFailure("non_public_address", blocked=True)

    proxy = PublicBrowserProxy(resolver, validate_url, 2, 10000)
    async with proxy:
        async with httpx.AsyncClient(proxy=proxy.url, trust_env=False) as client:
            with pytest.raises(httpx.TransportError):
                await client.get("https://127.0.0.1/private")
        assert proxy.failure == "browser_destination_blocked"
    assert not proxy.tasks


async def test_browser_proxy_preserves_host_and_pins_public_address(monkeypatch):
    from search_router.reader.browser_proxy import PublicBrowserProxy

    captured = []

    async def resolver(host, port):
        assert host == "example.org"
        return ["93.184.216.34"]

    original_connect = asyncio.open_connection

    async def upstream(reader, writer):
        captured.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def connect(host, target_port, *args, **kwargs):
        if host == "93.184.216.34":
            assert target_port == 80
            return await original_connect("127.0.0.1", port)
        return await original_connect(host, target_port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", connect)
    try:
        async with PublicBrowserProxy(resolver, validate_url, 2, 10000) as proxy:
            async with httpx.AsyncClient(proxy=proxy.url, trust_env=False) as client:
                response = await client.get("http://example.org/article")
                assert response.text == "OK"
    finally:
        server.close()
        await server.wait_closed()
    assert b"GET /article HTTP/1.1" in captured[0]
    assert b"Host: example.org" in captured[0]


async def test_reader_falls_back_once_and_keeps_mdream_output(monkeypatch):
    reader = Reader(Settings())
    calls = []

    async def download(url):
        calls.append("httpx")
        raise httpx.ConnectError("private raw error")

    async def browser(url):
        calls.append("cloakbrowser")
        return b"<html><main><h1>Article</h1><p>Body.</p></main></html>", "text/html", url

    monkeypatch.setattr(reader, "download", download)
    monkeypatch.setattr(reader, "browser", browser)
    response = await reader.read(ReadRequest(url="https://example.org"))
    expected = await reader.extract(
        b"<html><main><h1>Article</h1><p>Body.</p></main></html>",
        "text/html",
        100000,
        "https://example.org",
    )
    assert response.status == "ok"
    assert response.text == expected["text"]
    assert calls == ["httpx", "cloakbrowser"]
    assert "httpx:read_transport_error" in response.warnings
    assert "private raw error" not in response.model_dump_json()


@pytest.mark.parametrize(
    "reason,blocked",
    [
        ("document_http_401", False),
        ("document_http_429", False),
        ("download_limit", False),
        ("challenge_page", False),
        ("non_public_address", True),
    ],
)
async def test_reader_does_not_escalate_access_denial_or_limits(monkeypatch, reason, blocked):
    reader = Reader(Settings())

    async def download(url):
        raise ReadFailure(reason, blocked=blocked)

    async def forbidden(url):
        pytest.fail("access denials, challenges and limits must not trigger browser retries")

    monkeypatch.setattr(reader, "download", download)
    monkeypatch.setattr(reader, "browser", forbidden)
    response = await reader.read(ReadRequest(url="https://example.org"))
    assert response.status == ("blocked" if blocked else "unavailable")
    assert reason in response.warnings


@pytest.mark.parametrize("minimal", [True, False])
async def test_httpx_403_tries_browser_once_preserving_conversion_mode(monkeypatch, minimal):
    reader = Reader(Settings())
    calls = []

    async def download(url):
        calls.append("httpx")
        raise ReadFailure("document_http_403")

    async def browser(url):
        calls.append("cloakbrowser")
        return (
            b"<main><p>Rendered article</p></main><aside>Additional details</aside>",
            "text/html",
            url,
        )

    monkeypatch.setattr(reader, "download", download)
    monkeypatch.setattr(reader, "browser", browser)
    response = await reader.read(ReadRequest(url="https://example.org", minimal=minimal))
    assert response.status == "ok"
    assert calls == ["httpx", "cloakbrowser"]
    assert "Rendered article" in response.text
    assert ("Additional details" in response.text) == (not minimal)
    assert "httpx:document_http_403" in response.warnings
    assert "reader_backend:cloakbrowser" in response.warnings
    assert reader.active == 0


@pytest.mark.parametrize(
    "backends",
    [
        ["httpx", "cloakbrowser"],
        ["cloakbrowser", "httpx"],
        ["httpx"],
    ],
)
async def test_403_does_not_retry_denied_browser_or_enable_disabled_browser(monkeypatch, backends):
    reader = Reader(Settings(reader_backends=backends))
    calls = []

    async def download(url):
        calls.append("httpx")
        raise ReadFailure("document_http_403")

    async def browser(url):
        calls.append("cloakbrowser")
        raise ReadFailure("document_http_403")

    monkeypatch.setattr(reader, "download", download)
    monkeypatch.setattr(reader, "browser", browser)
    response = await reader.read(ReadRequest(url="https://example.org"))
    assert response.status == "unavailable"
    assert calls == (["cloakbrowser"] if backends[0] == "cloakbrowser" else backends)
    assert response.warnings[-1] == "document_http_403"
    assert reader.active == 0


async def test_empty_html_tries_browser_without_changing_conversion(monkeypatch):
    reader = Reader(Settings())

    async def download(url):
        return b"<html><body><div id='app'></div></body></html>", "text/html", url

    async def browser(url):
        return b"<html><main><p>Rendered article</p></main></html>", "text/html", url

    monkeypatch.setattr(reader, "download", download)
    monkeypatch.setattr(reader, "browser", browser)
    response = await reader.read(ReadRequest(url="https://example.org"))
    assert response.status == "ok"
    assert "Rendered article" in response.text
    assert "httpx:empty_extraction" in response.warnings


async def test_reader_backends_accept_only_supported_unique_names():
    from pydantic import ValidationError

    assert Settings(reader_backends=["cloakbrowser", "httpx"]).reader_backends == [
        "cloakbrowser",
        "httpx",
    ]
    for backends in (["removed-backend"], [], ["httpx", "httpx"]):
        with pytest.raises(ValidationError):
            Settings(reader_backends=backends)


async def test_html_charset_and_mdream_preserve_traditional_chinese():
    from search_router.reader.extract import decode_text

    html = "<html><head><meta charset='big5'></head><main><p>\u65b0\u805e\u5167\u5bb9</p></main></html>"
    decoded = decode_text(html.encode("big5"))
    assert decoded == html
    converted = await Reader(Settings()).extract(decoded.encode(), "text/html", 100000)
    assert "\u65b0\u805e\u5167\u5bb9" in converted["text"]


async def test_missing_node_returns_static_diagnostic():
    reader = Reader(Settings(reader_node_executable="missing-node-search-router-test"))
    with pytest.raises(ReadFailure, match="reader_runtime_missing"):
        await reader.extract(b"<html><main>Article</main></html>", "text/html", 100000)


@pytest.mark.skipif(
    os.getenv("ROUTER_BROWSER_LOCAL_SMOKE") != "1", reason="opt-in installed-browser local test"
)
async def test_real_cloakbrowser_cannot_fetch_private_subresource(monkeypatch):
    private_hits = []
    original_connect = asyncio.open_connection
    port = 0

    async def fixture(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        if b"/private" in request.split(b"\r\n", 1)[0]:
            private_hits.append(True)
        html = (
            "<html><main><h1>Local article</h1></main>"
            f"<iframe src='http://127.0.0.1:{port}/private'></iframe></html>"
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: "
            + str(len(html)).encode()
            + b"\r\n\r\n"
            + html
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(fixture, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def resolver(host, target_port):
        if host != "public-fixture.example":
            raise ReadFailure("non_public_address", blocked=True)
        return ["93.184.216.34"]

    async def connect(host, target_port, *args, **kwargs):
        if host == "93.184.216.34":
            return await original_connect("127.0.0.1", port)
        return await original_connect(host, target_port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", connect)
    try:
        reader = Reader(Settings(reader_backend_timeout=20), resolver=resolver)
        with pytest.raises(ReadFailure, match="browser_destination_blocked"):
            await reader.browser("http://public-fixture.example/article")
        assert not private_hits
    finally:
        server.close()
        await server.wait_closed()


async def test_reader_cancellation_releases_admission(monkeypatch):
    reader = Reader(Settings())
    entered = asyncio.Event()

    async def download(url):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(reader, "download", download)
    task = asyncio.create_task(reader.read(ReadRequest(url="https://example.org")))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.active == 0


async def test_backend_timeouts_do_not_retry_forever(monkeypatch):
    reader = Reader(Settings(reader_backend_timeout=0.01))
    calls = []

    async def stalled(url):
        calls.append(url)
        await asyncio.Event().wait()

    monkeypatch.setattr(reader, "download", stalled)
    monkeypatch.setattr(reader, "browser", stalled)
    response = await reader.read(ReadRequest(url="https://example.org"))
    assert response.status == "unavailable"
    assert len(calls) == 2
    assert "httpx:read_timeout" in response.warnings
    assert "cloakbrowser:read_timeout" in response.warnings
    assert reader.active == 0


async def test_worker_cancellation_kills_and_awaits_process(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    entered = asyncio.Event()
    killed = []
    process = SimpleNamespace(
        stdin=SimpleNamespace(write=lambda data: None, drain=AsyncMock(), close=lambda: None),
        stdout=asyncio.StreamReader(),
        returncode=None,
        wait=AsyncMock(),
    )

    def kill():
        killed.append(True)
        process.returncode = -9

    process.kill = kill

    async def spawn(*args, **kwargs):
        entered.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(Reader(Settings()).worker(["worker"], b"private", 1000, "failed"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [True]
    process.wait.assert_awaited()
