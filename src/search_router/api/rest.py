from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import contracts
from ..application import Gateway
from ..contracts import (
    Capabilities,
    DiscoveryOutcome,
    ErrorResponse,
    ProviderList,
    ProviderOutcome,
    QuotaOutcome,
    ReadRequest,
    ReadResponse,
    SearchOutcome,
    SearchRequest,
)
from .auth import auth_scope


def response(result, status=200):
    if isinstance(result, ErrorResponse):
        status = {
            "unknown_provider": 404,
            "unknown_schema": 404,
            "unsupported_operation": 409,
            "unknown_profile": 422,
        }.get(result.error, 503)
    return JSONResponse(result.model_dump(mode="json"), status_code=status)


def register_rest(app: FastAPI, gateway: Gateway):
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse(
            ErrorResponse(
                error="invalid_request",
                fields=[
                    {"location": list(item["loc"]), "type": item["type"]} for item in error.errors()
                ],
            ).model_dump(mode="json"),
            status_code=422,
        )

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        runtime = gateway.runtime
        configured = runtime.started and any(
            provider.capabilities().configured and runtime.settings.providers[provider.name].enabled
            for provider in runtime.instances
        )
        ready = bool(app.state.started and configured)
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    @app.post("/search", response_model=SearchOutcome)
    async def search(request: SearchRequest):
        result = await gateway.search(request, auth_scope.get())
        return response(result, 503 if getattr(result, "status", None) == "unavailable" else 200)

    @app.post("/read", response_model=ReadResponse)
    async def read(request: ReadRequest):
        result = await gateway.read(request)
        return response(
            result,
            {"ok": 200, "unsupported": 422, "blocked": 400, "unavailable": 503}[result.status],
        )

    @app.get("/capabilities", response_model=Capabilities)
    async def capabilities():
        return gateway.capabilities()

    @app.get("/providers", response_model=ProviderList)
    async def providers():
        return gateway.providers()

    @app.get("/providers/{name}", response_model=ProviderOutcome)
    async def provider(name: str):
        return response(gateway.provider(name))

    @app.post("/providers/{name}/discover", response_model=DiscoveryOutcome)
    async def discover(name: str):
        return response(await gateway.discover(name))

    @app.get("/providers/{name}/quotas", response_model=QuotaOutcome)
    async def quotas(name: str):
        return response(gateway.quotas(name))

    @app.get("/schemas/{name}")
    async def schema(name: str):
        model = getattr(contracts, name, None)
        if (
            not isinstance(model, type)
            or not issubclass(model, BaseModel)
            or model.__module__ != contracts.__name__
            or model is contracts.Contract
        ):
            return response(ErrorResponse(error="unknown_schema"))
        return model.model_json_schema()
