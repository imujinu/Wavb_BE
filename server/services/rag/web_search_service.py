from typing import Any

from schemas.rag import RetrievedSource
from settings import get_settings


class WebSearchConfigurationError(Exception):
    """Web search provider is not configured."""


class WebSearchProviderError(Exception):
    """Web search provider call failed."""


class WebSearchService:
    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.tavily_api_key
        self._max_results = settings.web_search_max_results
        self._client: Any | None = None

    # 기능 요약: Tavily 검색 결과를 공통 RetrievedSource 모델로 정규화한다.
    async def search(
        self,
        query: str,
        max_results: int | None = None,
    ) -> list[RetrievedSource]:
        if not self._api_key:
            raise WebSearchConfigurationError("TAVILY_API_KEY is not configured.")

        client = self._get_client()
        limit = (
            self._max_results
            if max_results is None
            else min(max_results, self._max_results)
        )
        try:
            response = await client.search(
                query=query,
                max_results=limit,
                include_answer=False,
                include_raw_content=False,
            )
        except Exception as exc:
            raise WebSearchProviderError("Tavily search failed.") from exc

        return self._to_sources(response)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from tavily import AsyncTavilyClient
        except Exception as exc:
            raise WebSearchConfigurationError(
                "tavily-python is required for web search."
            ) from exc
        self._client = AsyncTavilyClient(api_key=self._api_key)
        return self._client

    def _to_sources(self, response: Any) -> list[RetrievedSource]:
        results = self._extract_results(response)
        sources: list[RetrievedSource] = []
        for result in results:
            title = self._get_value(result, "title") or "Web result"
            snippet = self._get_value(result, "content") or ""
            url = self._get_value(result, "url")
            if not snippet and not url:
                continue
            sources.append(
                RetrievedSource(
                    source_type="web",
                    title=str(title),
                    snippet=str(snippet),
                    transcript_id=None,
                    url=str(url) if url else None,
                    score=self._to_float_or_none(self._get_value(result, "score")),
                    metadata={
                        "provider": "tavily",
                        "published_date": self._get_value(result, "published_date"),
                    },
                )
            )
        return sources

    def _extract_results(self, response: Any) -> list[Any]:
        if isinstance(response, dict):
            results = response.get("results", [])
        else:
            results = getattr(response, "results", [])
        return results if isinstance(results, list) else []

    def _get_value(self, value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    def _to_float_or_none(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
