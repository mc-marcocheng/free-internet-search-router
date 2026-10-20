import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..application import Gateway
from ..config import BootstrapSettings, Settings
from ..runtime import Runtime
from ..storage import open_store
from ..telemetry import configure
from .auth import Authentication
from .management import ConfigurationController, register_management
from .mcp import register_mcp
from .rest import register_rest


def create_app(
    settings: Settings | None = None, providers=None, reader=None, store=None
) -> FastAPI:
    if settings is None:
        store = store or open_store(BootstrapSettings())
        settings = store.load()[1]
    if not settings.tokens or any(
        len(value.get_secret_value()) < 24 or "REPLACE_" in value.get_secret_value()
        for value in settings.tokens.values()
    ):
        raise RuntimeError("Initialize protected router tokens with search-router init")
    for name in ("httpx", "httpcore", "mcp", "pypdf"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)
    runtime = Runtime(settings, providers, reader)
    gateway = Gateway(runtime)
    controller = ConfigurationController(store, gateway) if store else None
    hosts = [entry for host in settings.allowed_hosts for entry in (host, host + ":*")]
    origins = [scheme + "://" + host for scheme in ("http", "https") for host in hosts]
    mcp = FastMCP(
        "Free Internet Search Router",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins),
    )

    @asynccontextmanager
    async def lifespan(app):
        exporters = None
        watcher = None
        try:
            exporters = configure(settings.otlp_endpoint)
            await runtime.start()
            app.state.router, app.state.quotas = runtime.router, runtime.quotas
            if controller:
                watcher = asyncio.create_task(controller.watch())
            async with mcp.session_manager.run():
                app.state.started = True
                yield
        finally:
            app.state.started = False
            try:
                if controller:
                    controller.stop.set()
                if watcher:
                    await watcher
                await gateway.runtime.close()
            finally:
                if exporters:
                    for exporter in exporters:
                        exporter.shutdown()

    app = FastAPI(title="Free Internet Search Router", lifespan=lifespan)
    app.state.runtime, app.state.gateway, app.state.mcp = runtime, gateway, mcp
    app.state.started = False
    app.add_middleware(
        Authentication, settings=settings, gateway=gateway, management=store is not None
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    register_rest(app, gateway)
    register_mcp(mcp, gateway)
    if controller:
        app.state.configuration = controller
        register_management(app, controller)
    app.mount("/", mcp.streamable_http_app())
    return app
