import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx

from ..config import Settings
from ..contracts import ReadRequest, ReadResponse
from .security import ReadFailure, resolve_public, validate_url


class Reader:
    def __init__(self, settings: Settings, client_factory=None, resolver=resolve_public):
        self.settings = settings
        self.client_factory = client_factory or httpx.AsyncClient
        self.resolver = resolver
        self.active = 0

    async def read(self, request: ReadRequest) -> ReadResponse:
        response = ReadResponse(status="unavailable", original_url=request.url)
        if self.active >= self.settings.reader_concurrency:
            response.warnings = ["reader_busy"]
            return response
        self.active += 1
        try:
            async with asyncio.timeout(self.settings.reader_timeout):
                for backend in self.settings.reader_backends:
                    phase = "fetch"
                    try:
                        async with asyncio.timeout(self.settings.reader_backend_timeout):
                            if backend == "httpx":
                                content, content_type, final = await self.download(request.url)
                            else:
                                content, content_type, final = await self.browser(request.url)
                            response.final_url, response.content_type = final, content_type
                            if content_type not in {
                                "text/html",
                                "application/xhtml+xml",
                                "text/plain",
                                "application/pdf",
                            }:
                                response.status = "unsupported"
                                response.warnings.append("unsupported_content_type")
                                return response
                            phase = "conversion"
                            extracted = await self.extract(
                                content,
                                content_type,
                                request.max_chars,
                                final,
                                minimal=request.minimal,
                            )
                            if not extracted["text"]:
                                raise ReadFailure("empty_extraction")
                            response.text = extracted["text"]
                            response.truncated = extracted["truncated"]
                            response.warnings.extend(
                                extracted["warnings"] + ["reader_backend:" + backend]
                            )
                            response.status = "ok"
                            return response
                    except (TimeoutError, httpx.TimeoutException):
                        failure = ReadFailure(
                            "conversion_timeout" if phase == "conversion" else "read_timeout"
                        )
                    except httpx.TransportError:
                        failure = ReadFailure("read_transport_error")
                    except ReadFailure as error:
                        failure = error
                    response.warnings.append(backend + ":" + failure.reason)
                    if failure.blocked:
                        response.status, response.original_url = "blocked", "[blocked URL]"
                        response.final_url = None
                        response.warnings.append(failure.reason)
                        return response
                    if (
                        failure.reason
                        not in {
                            "read_timeout",
                            "read_transport_error",
                            "empty_extraction",
                            "compressed_document_unsupported",
                            "browser_navigation_failed",
                        }
                        and not failure.reason.startswith("document_http_5")
                        and not (backend == "httpx" and failure.reason == "document_http_403")
                    ):
                        response.warnings.append(failure.reason)
                        return response
                response.warnings.append(failure.reason)
        except asyncio.CancelledError:
            raise
        except ReadFailure as error:
            response.status = "blocked" if error.blocked else "unavailable"
            response.warnings = [error.reason]
            if error.blocked:
                response.original_url = "[blocked URL]"
        except (TimeoutError, httpx.TimeoutException):
            response.warnings = ["read_timeout"]
        except httpx.TransportError:
            response.warnings = ["read_transport_error"]
        except Exception:
            response.warnings = ["read_failed"]
        finally:
            self.active -= 1
        return response

    async def download(self, original: str) -> tuple[bytes, str, str]:
        current = original
        async with self.client_factory(
            timeout=self.settings.reader_timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "internet-search-router/0.1", "Accept-Encoding": "identity"},
        ) as client:
            for redirect in range(6):
                client.cookies.clear()
                url = validate_url(current)
                addresses = await self.resolver(
                    url.host, url.port or (443 if url.scheme == "https" else 80)
                )
                pinned = url.copy_with(host=addresses[0])
                headers = {"Host": url.netloc.decode("ascii")}
                async with client.stream(
                    "GET", pinned, headers=headers, extensions={"sni_hostname": url.host}
                ) as upstream:
                    if upstream.status_code in {301, 302, 303, 307, 308}:
                        location = upstream.headers.get("location")
                        if not location:
                            raise ReadFailure("invalid_redirect")
                        current = urljoin(str(url), location)
                        continue
                    if upstream.status_code >= 300:
                        raise ReadFailure(f"document_http_{upstream.status_code}")
                    if upstream.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ReadFailure("compressed_document_unsupported")
                    length = upstream.headers.get("content-length")
                    if length and (
                        not length.isdigit() or int(length) > self.settings.reader_limit_bytes
                    ):
                        raise ReadFailure("download_limit")
                    content = bytearray()
                    async for chunk in upstream.aiter_raw():
                        content.extend(chunk)
                        if len(content) > self.settings.reader_limit_bytes:
                            raise ReadFailure("download_limit")
                    content_type = (
                        upstream.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                    )
                    if content_type in {"text/plain", "text/html", "application/xhtml+xml"}:
                        from .extract import decode_text

                        try:
                            decoded = decode_text(
                                bytes(content),
                                upstream.charset_encoding,
                                html=content_type != "text/plain",
                            )
                        except (LookupError, UnicodeError):
                            raise ReadFailure("unsupported_text_encoding") from None
                        content = bytearray(decoded.encode("utf-8"))
                    return bytes(content), content_type, str(url)
        raise ReadFailure("redirect_limit")

    async def worker(
        self, command: list[str], content: bytes, output_limit: int, reason: str, tree: bool = False
    ) -> dict:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=tree and os.name != "nt",
            )
        except FileNotFoundError:
            raise ReadFailure("reader_runtime_missing") from None

        async def send():
            try:
                process.stdin.write(content)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        async def receive():
            output = bytearray()
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > output_limit:
                    raise ReadFailure("extraction_output_limit")
            return bytes(output)

        sending = asyncio.create_task(send())
        receiving = asyncio.create_task(receive())
        try:
            _, output = await asyncio.gather(sending, receiving)
            await process.wait()
            if process.returncode:
                raise ReadFailure(reason)
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError):
                raise ReadFailure(reason) from None
            if not isinstance(result, dict):
                raise ReadFailure(reason)
            return result
        finally:
            for task in (sending, receiving):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sending, receiving, return_exceptions=True)
            if process.returncode is None:
                if tree and os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
                elif tree:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
            await process.wait()

    async def browser(self, original: str) -> tuple[bytes, str, str]:
        from .browser_proxy import PublicBrowserProxy

        url = validate_url(original)
        await self.resolver(url.host, url.port or (443 if url.scheme == "https" else 80))
        async with PublicBrowserProxy(
            self.resolver,
            validate_url,
            self.settings.reader_backend_timeout,
            self.settings.reader_limit_bytes * 8,
        ) as proxy:
            bridge = Path(__file__).parent / "node" / "reader.mjs"
            result = await self.worker(
                [self.settings.reader_node_executable, str(bridge)],
                json.dumps(
                    {
                        "mode": "browser",
                        "url": str(url),
                        "proxy": proxy.url,
                        "timeout": int(self.settings.reader_backend_timeout * 1000),
                        "limit": self.settings.reader_limit_bytes,
                    }
                ).encode(),
                self.settings.reader_limit_bytes * 8 + 10000,
                "browser_navigation_failed",
                tree=True,
            )
            if proxy.failure in {
                "browser_destination_blocked",
                "browser_protocol_blocked",
                "browser_request_limit",
                "browser_traffic_limit",
            }:
                raise ReadFailure(
                    proxy.failure, blocked=proxy.failure == "browser_destination_blocked"
                )
        if "error" in result:
            reason = result["error"]
            if reason not in {
                "browser_not_installed",
                "browser_navigation_failed",
                "browser_request_limit",
                "download_limit",
            } and not (
                isinstance(reason, str)
                and reason.startswith("document_http_")
                and reason.removeprefix("document_http_").isdigit()
            ):
                reason = "browser_navigation_failed"
            raise ReadFailure(reason)
        html, final = result.get("html"), result.get("final_url")
        if not isinstance(html, str) or not isinstance(final, str):
            raise ReadFailure("browser_navigation_failed")
        destination = validate_url(final)
        await self.resolver(
            destination.host, destination.port or (443 if destination.scheme == "https" else 80)
        )
        content = html.encode("utf-8")
        if len(content) > self.settings.reader_limit_bytes:
            raise ReadFailure("download_limit")
        return content, "text/html", str(destination)

    async def extract(
        self,
        content: bytes,
        content_type: str,
        max_chars: int,
        origin: str = "",
        minimal: bool = True,
    ) -> dict:
        if content_type in {"text/html", "application/xhtml+xml"}:
            bridge = Path(__file__).parent / "node" / "reader.mjs"
            result = await self.worker(
                [self.settings.reader_node_executable, str(bridge)],
                json.dumps(
                    {
                        "html": content.decode("utf-8"),
                        "origin": origin,
                        "minimal": minimal,
                    }
                ).encode(),
                self.settings.reader_limit_bytes * 12 + 10000,
                "mdream_conversion_failed",
            )
            text = result.get("text")
            if not isinstance(text, str):
                raise ReadFailure("mdream_conversion_failed")
            if any(
                marker in text[:20000].casefold()
                for marker in (
                    "verify you are human",
                    "just a moment...",
                    "captcha",
                )
            ):
                raise ReadFailure("challenge_page")
            return {
                "text": text[:max_chars],
                "truncated": len(text) > max_chars,
                "warnings": [
                    "html_to_markdown_mdream_standard_fallback"
                    if result.get("fallback")
                    else "html_to_markdown_mdream_minimal"
                    if minimal
                    else "html_to_markdown_mdream_standard"
                ],
            }
        return await self.worker(
            [
                sys.executable,
                "-m",
                "search_router.reader.extract",
                content_type,
                str(max_chars),
                str(self.settings.reader_max_pages),
            ],
            content,
            max_chars * 12 + 10000,
            "extraction_failed",
        )
