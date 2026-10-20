from mcp.server.fastmcp import FastMCP

from ..application import Gateway
from ..contracts import (
    Capabilities,
    DiscoveryOutcome,
    ProviderList,
    ProviderOutcome,
    QuotaOutcome,
    ReadRequest,
    ReadResponse,
    SearchOutcome,
    SearchRequest,
)
from .auth import auth_scope


def register_mcp(mcp: FastMCP, gateway: Gateway):
    @mcp.tool(name="search_web")
    async def search(request: SearchRequest) -> SearchOutcome:
        """Search with explicit constraints, provenance and upstream accounting."""
        return SearchOutcome(await gateway.search(request, auth_scope.get()))

    @mcp.tool(name="read_url")
    async def read(request: ReadRequest) -> ReadResponse:
        """Read a public HTML, text or PDF document independently of search."""
        return await gateway.read(request)

    @mcp.tool(name="get_capabilities")
    async def capabilities() -> Capabilities:
        """Return configured provider constraints and reader capabilities."""
        return gateway.capabilities()

    @mcp.tool(name="get_providers")
    async def providers() -> ProviderList:
        """Return provider health, key status and routing budget status."""
        return gateway.providers()

    @mcp.tool(name="get_provider")
    async def provider(name: str) -> ProviderOutcome:
        """Return one provider's capabilities, supported operations and health."""
        return ProviderOutcome(gateway.provider(name))

    @mcp.tool(name="discover_provider")
    async def discover(name: str) -> DiscoveryOutcome:
        """Discover an adapter's advertised tools or engine inventory."""
        return DiscoveryOutcome(await gateway.discover(name))

    @mcp.tool(name="get_provider_quotas")
    async def quotas(name: str) -> QuotaOutcome:
        """Read cached quota observations without making an upstream request."""
        return QuotaOutcome(gateway.quotas(name))
