import asyncio
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from ..key_checks import KeyAction, KeyChecks, key_status
from ..management import ConfigurationUpdate, CredentialUpdate, Management
from ..storage import ConfigurationError


class ConfigurationController:
    def __init__(self, store, gateway):
        self.management = Management(store)
        self.gateway = gateway
        self.revision = store.load()[0]
        self.startup = gateway.runtime.settings
        self.error = None
        self.lock = asyncio.Lock()
        self.stop = asyncio.Event()

    def status(self):
        document = self.management.store.public()
        document.update(
            key_status=key_status(self.gateway.runtime)
            if self.gateway.runtime.started and document["revision"] == self.revision
            else {},
            active_revision=self.revision,
            activation_error=self.error,
            restart_required=[
                name
                for name in ("allowed_hosts", "otlp_endpoint")
                if document["configuration"][name] != getattr(self.startup, name)
            ],
        )
        return document

    async def activate(self):
        revision, settings = self.management.store.load()
        if revision != self.revision:
            await self.gateway.reconfigure(settings)
            self.revision = revision
            self.error = None
            if hasattr(self, "app"):
                self.app.state.runtime = self.gateway.runtime
                self.app.state.router = self.gateway.runtime.router
                self.app.state.quotas = self.gateway.runtime.quotas

    async def watch(self):
        while not self.stop.is_set():
            try:
                async with self.lock:
                    await self.activate()
            except Exception:
                self.error = "configuration_activation_failed"
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1)
            except TimeoutError:
                pass


def register_management(app, controller):
    web = Path(__file__).with_name("web")
    controller.app = app

    @app.middleware("http")
    async def management_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/admin"):
            response.headers.update(
                {
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "X-Frame-Options": "DENY",
                    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                }
            )
        return response

    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        return FileResponse(web / "index.html")

    @app.get("/admin/{asset}", include_in_schema=False)
    async def admin_asset(asset: str):
        if asset not in {"app.js", "style.css", "icons.svg", "public-sans.woff2"}:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return FileResponse(web / asset)

    @app.get("/admin/api/configuration")
    async def configuration():
        try:
            return controller.status()
        except ConfigurationError:
            return JSONResponse({"error": "configuration_unavailable"}, status_code=503)

    async def change(update, method):
        try:
            async with controller.lock:
                method(update)
                try:
                    await controller.activate()
                except Exception:
                    controller.error = "configuration_activation_failed"
                return controller.status()
        except ConfigurationError as error:
            code = str(error)
            return JSONResponse(
                {"error": code}, status_code=409 if code == "configuration_conflict" else 422
            )

    @app.put("/admin/api/configuration")
    async def update_configuration(update: ConfigurationUpdate):
        return await change(update, controller.management.configure)

    @app.post("/admin/api/credentials")
    async def update_credentials(update: CredentialUpdate):
        return await change(update, controller.management.credential)

    @app.get("/admin/api/keys/{provider}")
    async def provider_keys(provider: str):
        async with controller.lock:
            if controller.management.store.load()[0] != controller.revision:
                return JSONResponse({"error": "configuration_conflict"}, status_code=409)
            document = controller.status()
            if document["revision"] != controller.revision:
                return JSONResponse({"error": "configuration_conflict"}, status_code=409)
            if provider not in document["key_metadata"]:
                return JSONResponse({"error": "unknown_provider"}, status_code=404)
            return {
                "revision": controller.revision,
                "keys": document["key_metadata"][provider],
                "status": document["key_status"].get(provider, []),
            }

    @app.post("/admin/api/key-actions")
    async def key_action(action: KeyAction):
        async with controller.lock:
            if (
                action.revision != controller.revision
                or controller.management.store.load()[0] != action.revision
            ):
                return JSONResponse({"error": "configuration_conflict"}, status_code=409)
            async with controller.gateway.operation():
                return await KeyChecks(controller.gateway.runtime).run(action)
