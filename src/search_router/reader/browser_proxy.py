import asyncio
from urllib.parse import urlsplit


class PublicBrowserProxy:
    def __init__(self, resolver, validator, timeout: float, limit: int):
        self.resolver, self.validator = resolver, validator
        self.timeout, self.limit = timeout, limit
        self.transferred = 0
        self.connections = 0
        self.tasks: set[asyncio.Task] = set()
        self.failure: str | None = None
        self.server = None

    def fail(self, reason: str):
        priority = {
            "browser_transport_error": 0,
            "browser_protocol_blocked": 1,
            "browser_request_limit": 1,
            "browser_traffic_limit": 1,
            "browser_destination_blocked": 2,
        }
        if self.failure is None or priority[reason] > priority[self.failure]:
            self.failure = reason

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=16384)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        try:
            self.connections += 1
            if self.connections > 64:
                self.fail("browser_request_limit")
                return
            async with asyncio.timeout(self.timeout):
                headers = await reader.readuntil(b"\r\n\r\n")
                lines = headers.decode("iso-8859-1").split("\r\n")
                method, target, version = lines[0].split(" ")
                if method not in {"CONNECT", "GET", "HEAD"} or version != "HTTP/1.1":
                    self.fail("browser_protocol_blocked")
                    return
                url = self.validator("https://" + target if method == "CONNECT" else target)
                port = url.port or (443 if url.scheme == "https" else 80)
                addresses = await self.resolver(url.host, port)
                incoming, upstream = await asyncio.open_connection(addresses[0], port)
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    parsed = urlsplit(str(url))
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    forwarded = [f"{method} {path} HTTP/1.1", "Host: " + url.netloc.decode("ascii")]
                    for line in lines[1:]:
                        if not line:
                            continue
                        name, separator, value = line.partition(":")
                        if not separator or name.lower() in {"content-length", "transfer-encoding"}:
                            self.fail("browser_protocol_blocked")
                            return
                        if name.lower() not in {
                            "host",
                            "connection",
                            "proxy-connection",
                            "proxy-authorization",
                        }:
                            forwarded.append(name + ":" + value)
                    forwarded.append("Connection: close")
                    upstream.write(("\r\n".join(forwarded) + "\r\n\r\n").encode("iso-8859-1"))
                    await upstream.drain()
                relays = [
                    asyncio.create_task(self.relay(reader, upstream)),
                    asyncio.create_task(self.relay(incoming, writer)),
                ]
                try:
                    await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for relay in relays:
                        relay.cancel()
                    await asyncio.gather(*relays, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail(
                "browser_destination_blocked"
                if getattr(error, "blocked", False)
                else "browser_transport_error"
            )
        finally:
            for stream in (upstream, writer):
                if stream:
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except (OSError, RuntimeError):
                        pass
            self.tasks.discard(task)

    async def relay(self, reader, writer):
        while chunk := await reader.read(65536):
            self.transferred += len(chunk)
            if self.transferred > self.limit:
                self.fail("browser_traffic_limit")
                return
            writer.write(chunk)
            await writer.drain()
