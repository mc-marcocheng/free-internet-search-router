import asyncio
import hashlib
import hmac
from contextvars import ContextVar
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

from ..config import Settings

auth_scope: ContextVar[str] = ContextVar("auth_scope", default="")


class Authentication:
    def __init__(self, app, settings: Settings, gateway=None, management=False):
        self.app, self.settings, self.gateway = app, settings, gateway
        self.management = management

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {name.lower(): value for name, value in scope["headers"]}
        if scope["path"] in {"/health/live", "/health/ready"} and scope["method"] == "GET":
            return await self.app(scope, receive, send)
        if (
            self.management
            and scope["path"]
            in {
                "/admin",
                "/admin/app.js",
                "/admin/style.css",
                "/admin/icons.svg",
                "/admin/public-sans.woff2",
            }
            and scope["method"] == "GET"
        ):
            return await self.app(scope, receive, send)
        supplied = headers.get(b"authorization", b"")
        identity = None
        settings = self.gateway.runtime.settings if self.gateway is not None else self.settings
        for name, token in settings.tokens.items():
            expected = ("Bearer " + token.get_secret_value()).encode()
            if hmac.compare_digest(
                hashlib.sha256(supplied).digest(), hashlib.sha256(expected).digest()
            ):
                identity = name
        if identity is None:
            return await JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
        if scope["path"].startswith("/admin/api/") and identity not in settings.administrators:
            return await JSONResponse({"error": "administrator_required"}, status_code=403)(
                scope, receive, send
            )
        origin = headers.get(b"origin")
        if origin:
            try:
                parsed = urlsplit(origin.decode("ascii"))
                host = headers.get(b"host", b"").decode("ascii")
                valid = parsed.netloc == host and parsed.scheme in {"http", "https"}
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                return await JSONResponse({"error": "origin_not_allowed"}, status_code=403)(
                    scope, receive, send
                )
        content_length = headers.get(b"content-length", b"0")
        if not content_length.isdigit() or int(content_length) > 65536:
            return await JSONResponse({"error": "request_too_large"}, status_code=413)(
                scope, receive, send
            )
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            buffered = []
            size = 0
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        size += len(message.get("body", b""))
                        if size > 65536:
                            return await JSONResponse(
                                {"error": "request_too_large"}, status_code=413
                            )(scope, receive, send)
                        buffered.append(message)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await JSONResponse({"error": "request_body_timeout"}, status_code=408)(
                    scope, receive, send
                )
            original_receive = receive

            async def replay():
                return buffered.pop(0) if buffered else await original_receive()

            receive = replay
        token = auth_scope.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            auth_scope.reset(token)
