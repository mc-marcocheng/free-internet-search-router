import os

import httpx

from search_router.contracts import ReadRequest, ReadResponse, SearchRequest, SearchResponse


class SearchClient:
    def __init__(self, base_url: str, token: str):
        self.http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(55, connect=5),
            trust_env=False,
        )

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ.get("SEARCH_ROUTER_URL", "http://127.0.0.1:9092"),
            os.environ["SEARCH_ROUTER_TOKEN"],
        )

    async def search(self, request: SearchRequest) -> SearchResponse:
        upstream = await self.http.post("/search", json=request.model_dump(mode="json"))
        if upstream.status_code != 503:
            upstream.raise_for_status()
        return SearchResponse.model_validate(upstream.json())

    async def general(self, query: str, locale: str | None = None) -> SearchResponse:
        return await self.search(SearchRequest(query=query, locale=locale))

    async def news(self, query: str, window: str = "week") -> SearchResponse:
        if window not in {"day", "week"}:
            raise ValueError("integration news windows are day or week")
        return await self.search(
            SearchRequest(query=query, category="news", locale="en", time_range=window)
        )

    async def read_document(self, url: str, *, minimal: bool = True) -> ReadResponse:
        upstream = await self.http.post(
            "/read", json=ReadRequest(url=url, max_chars=1000000, minimal=minimal).model_dump()
        )
        upstream.raise_for_status()
        document = ReadResponse.model_validate(upstream.json())
        if document.status != "ok" or document.truncated:
            raise ValueError("complete source document unavailable; do not store as exact evidence")
        return document

    async def close(self):
        await self.http.aclose()
