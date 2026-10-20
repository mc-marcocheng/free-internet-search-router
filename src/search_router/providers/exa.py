import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

import httpx
import jsonschema
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator

from ..contracts import (
    CapabilityIssue,
    ProviderCapabilities,
    QuotaObservation,
    SearchRequest,
    utcnow,
)
from .base import Batch, ProviderError
from .config import CredentialConfig, validate_endpoint
from .errors import check_http, error_object
from .http import normalize, unsupported


class ExaConfig(CredentialConfig):
    url: str = "https://mcp.exa.ai/mcp?tools=web_search_exa,web_search_advanced_exa"
    keyless: bool = False
    service_key: SecretStr = Field(
        default=SecretStr(""), exclude=True, repr=False, max_length=16384
    )
    usage_key_ids: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{0,128}$")]] = (
        Field(default_factory=list)
    )
    monthly_budget_usd: float = Field(default=10, gt=0, allow_inf_nan=False)
    quota_unit = "usd"

    @model_validator(mode="after")
    def align_usage_ids(self):
        if len(self.usage_key_ids) > len(self.keys):
            raise ValueError("usage key IDs must correspond to configured keys")
        if len(set(filter(None, self.usage_key_ids))) != len(
            list(filter(None, self.usage_key_ids))
        ):
            raise ValueError("usage key IDs must be unique")
        self.usage_key_ids = [
            *self.usage_key_ids,
            *([""] * (len(self.keys) - len(self.usage_key_ids))),
        ]
        return self

    @field_validator("url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return validate_endpoint(value)

    def quota_identity(self) -> dict:
        return {
            "url": self.url,
            "service_key": hashlib.sha256(self.service_key.get_secret_value().encode()).hexdigest(),
            "usage_key_ids": self.usage_key_ids,
            "monthly_budget_usd": self.monthly_budget_usd,
        }


def mcp_payload(result, provider: str, tool: str) -> dict:
    if result.isError:
        text = " ".join(block.text for block in result.content if block.type == "text")
        error_object({"error": text or "upstream error"})
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    text = "\n".join(block.text for block in result.content if block.type == "text")
    if len(text) > 5000000:
        raise ProviderError("malformed_response")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        return payload
    if provider == "exa":
        if text.strip() in {
            "No search results found. Please try a different query.",
            "No search results found. Please try a different query or adjust your filters.",
        }:
            return {"results": []}
        if tool == "web_search_exa":
            rows = []
            for block in text.split("\n\n---\n\n"):
                match = re.fullmatch(
                    r"Title: (.*?)\nURL: (\S+)\nPublished: (.*?)\nAuthor: [^\n]*(?:\n(?:Highlights:\n|Text: )(.*))?",
                    block,
                    re.DOTALL,
                )
                if not match:
                    raise ProviderError("malformed_response")
                title, url, publication, content = match.groups()
                rows.append(
                    {"title": title, "url": url, "publishedDate": publication, "content": content}
                )
            return {"results": rows}
    raise ProviderError("malformed_response")


class ExaProvider:
    name = "exa"

    def __init__(
        self,
        settings: ExaConfig,
        client=None,
        api_key: SecretStr | None = None,
        timeout: float = 10,
        *,
        client_factory=None,
    ):
        self.settings = settings
        keys = settings.credentials()
        self.api_key = api_key or (keys[0] if keys else None)
        self.client = client
        self.client_factory = client_factory or httpx.AsyncClient
        self.timeout = timeout
        self.tools: dict = {}
        self.discovery_status = "not_discovered"

    @property
    def quota_configuration_error(self) -> str | None:
        if not self.settings.service_key.get_secret_value().strip():
            return "quota_service_key_required"
        if self.api_key is None or self.api_key not in self.settings.credentials():
            return "quota_key_id_required"
        index = self.settings.credentials().index(self.api_key)
        return None if self.settings.usage_key_ids[index] else "quota_key_id_required"

    async def quota_status(self) -> QuotaObservation:
        if self.quota_configuration_error:
            raise ProviderError(self.quota_configuration_error)
        index = self.settings.credentials().index(self.api_key)
        identifier = self.settings.usage_key_ids[index]
        now = utcnow().replace(microsecond=0)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        reset = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        response = await self.client.get(
            f"https://admin-api.exa.ai/team-management/api-keys/{identifier}/usage",
            headers={"x-api-key": self.settings.service_key.get_secret_value()},
            params={"start_date": start.isoformat(), "end_date": now.isoformat()},
            timeout=self.timeout,
            follow_redirects=False,
        )
        check_http(response)
        try:
            payload = response.json()
            cost = payload["total_cost_usd"]
            period = payload["period"]
            if (
                (payload.get("api_key_id") != identifier and payload.get("id") != identifier)
                or datetime.fromisoformat(period["start"]) != start
                or datetime.fromisoformat(period["end"]) != now
            ):
                raise ValueError("unexpected usage scope")
            if type(cost) not in {int, float} or not Decimal(str(cost)).is_finite() or cost < 0:
                raise ValueError("invalid cost")
            budget = Decimal(str(self.settings.monthly_budget_usd))
            return QuotaObservation(
                remaining=float(max(Decimal(0), budget - Decimal(str(cost)))),
                unit="usd",
                budget_usd=float(budget),
                used_usd=cost,
                observed_at=now,
                reset_at=reset,
                scope="key",
                source="configured_budget",
            )
        except (ValueError, KeyError, TypeError):
            raise ProviderError("malformed_response") from None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            configured=bool(self.api_key or self.settings.keyless),
            categories=["general", "news"],
            locales=[],
            time_ranges=["day", "week", "month", "year"],
            explicit_dates=True,
            diagnostics=[self.discovery_status],
        )

    def arguments(self, request: SearchRequest) -> tuple[str, dict]:
        if request.locale:
            raise unsupported(request, "locale", reason="language_filter_not_supported")
        advanced = bool(
            request.category == "news"
            or request.include_domains
            or request.exclude_domains
            or request.time_range
            or request.start_date
            or request.end_date
        )
        tool = "web_search_advanced_exa" if advanced else "web_search_exa"
        args = {"query": request.query, "numResults": max(10, request.limit)}
        if advanced:
            args.update(textMaxCharacters=2000, enableSummary=False)
            if request.category == "news":
                args["category"] = "news"
            for local, remote in (
                ("include_domains", "includeDomains"),
                ("exclude_domains", "excludeDomains"),
                ("start_date", "startPublishedDate"),
                ("end_date", "endPublishedDate"),
            ):
                value = getattr(request, local)
                if value:
                    args[remote] = str(value) if local.endswith("date") else value
            if request.time_range:
                days = {"day": 1, "week": 7, "month": 30, "year": 365}[request.time_range]
                args["startPublishedDate"] = (utcnow() - timedelta(days=days)).isoformat()
        return tool, args

    async def _session(self, operation):
        endpoint = self.settings.url
        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key.get_secret_value()
        async with self.client_factory(
            headers=headers,
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with streamable_http_client(endpoint, http_client=client) as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = {}
                    cursor = None
                    for page in range(20):
                        listing = await session.list_tools(cursor=cursor)
                        tools.update({tool.name: tool for tool in listing.tools})
                        cursor = listing.nextCursor
                        if not cursor:
                            break
                    else:
                        raise ProviderError("malformed_response")
                    self.tools = tools
                    self.discovery_status = "discovered"
                    return await operation(session)

    async def discover(self) -> dict:
        async def collect(session):
            return {
                "tools": [
                    {
                        "name": tool.name,
                        "input_schema": tool.inputSchema,
                        "output_schema": tool.outputSchema,
                    }
                    for tool in self.tools.values()
                ]
            }

        return await self._session(collect)

    async def search(self, request: SearchRequest, charge: Callable[[], Awaitable[None]]) -> Batch:
        if not self.capabilities().configured:
            raise ProviderError("unconfigured")
        tool_name, args = self.arguments(request)

        async def call(session):
            tool = self.tools.get(tool_name)
            if not tool:
                raise ProviderError(
                    "unsupported_capability",
                    diagnostics=[
                        CapabilityIssue(
                            field="tool", value=tool_name, reason="required_tool_not_advertised"
                        )
                    ],
                )
            properties = tool.inputSchema.get("properties", {})
            if not isinstance(properties, dict):
                raise ProviderError(
                    "unsupported_capability",
                    diagnostics=[
                        CapabilityIssue(
                            field="tool_schema",
                            reason="invalid_tool_schema",
                        )
                    ],
                )
            fields = {
                "query": "query",
                "numResults": "limit",
                "category": "category",
                "includeDomains": "include_domains",
                "excludeDomains": "exclude_domains",
                "startPublishedDate": "time_range" if request.time_range else "start_date",
                "endPublishedDate": "end_date",
            }

            def schema_issue(parameter, reason):
                local = fields.get(parameter)
                if local and local != "query":
                    return unsupported(request, local, reason=reason)
                safe_parameter = (
                    parameter
                    if isinstance(parameter, str)
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", parameter)
                    else "tool_schema"
                )
                return ProviderError(
                    "unsupported_capability",
                    diagnostics=[
                        CapabilityIssue(
                            field=safe_parameter,
                            value=(
                                str(args[parameter])
                                if parameter in {"textMaxCharacters", "enableSummary"}
                                else None
                            ),
                            reason=reason,
                        )
                    ],
                )

            if "objective" in properties:
                args["objective"] = (
                    "Find documents relevant to the query, exclude unrelated documents, and return supporting facts."
                )
            missing = [name for name in args if name not in properties]
            if missing:
                failure = schema_issue(missing[0], "parameter_not_advertised")
                for parameter in missing[1:]:
                    failure.diagnostics.extend(
                        schema_issue(parameter, "parameter_not_advertised").diagnostics
                    )
                raise failure

            def local_references(value):
                if isinstance(value, dict):
                    if "$ref" in value and not str(value["$ref"]).startswith("#"):
                        return False
                    return all(local_references(item) for item in value.values())
                if isinstance(value, list):
                    return all(local_references(item) for item in value)
                return True

            if not local_references(tool.inputSchema):
                raise schema_issue(None, "external_schema_reference_not_supported")
            try:
                jsonschema.Draft202012Validator(tool.inputSchema).validate(args)
            except jsonschema.ValidationError as error:
                parameter = next(iter(error.absolute_path), None)
                if error.validator == "required" and not parameter:
                    missing = [name for name in error.validator_value if name not in args]
                    failure = ProviderError("unsupported_capability")
                    for parameter in missing:
                        failure.diagnostics.extend(
                            schema_issue(parameter, "required_parameter_not_supported").diagnostics
                        )
                    raise failure from None
                raise schema_issue(parameter, "schema_value_not_supported") from None
            except jsonschema.SchemaError:
                raise schema_issue(None, "invalid_tool_schema") from None
            await charge()
            result = await session.call_tool(tool_name, args)
            payload = mcp_payload(result, self.name, tool_name)
            return normalize(payload, self.name)

        return await self._session(call)
