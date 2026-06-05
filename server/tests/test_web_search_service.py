import pytest

from services.rag.web_search_service import (
    WebSearchConfigurationError,
    WebSearchService,
)


class FakeTavilyClient:
    def __init__(self) -> None:
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "results": [
                {
                    "title": "Example title",
                    "content": "Example content",
                    "url": "https://example.com",
                    "score": 1.2,
                    "published_date": "2026-06-04",
                }
            ]
        }


def make_service(api_key: str = "test-key") -> tuple[WebSearchService, FakeTavilyClient]:
    service = WebSearchService.__new__(WebSearchService)
    client = FakeTavilyClient()
    service._api_key = api_key
    service._max_results = 5
    service._client = client
    return service, client


@pytest.mark.asyncio
async def test_web_search_maps_tavily_results_to_sources() -> None:
    service, client = make_service()

    sources = await service.search("query", max_results=3)

    assert client.calls[0]["query"] == "query"
    assert client.calls[0]["max_results"] == 3
    assert client.calls[0]["include_answer"] is False
    assert client.calls[0]["include_raw_content"] is False
    assert len(sources) == 1
    assert sources[0].source_type == "web"
    assert sources[0].title == "Example title"
    assert sources[0].snippet == "Example content"
    assert sources[0].url == "https://example.com"
    assert sources[0].score == 1.2
    assert sources[0].metadata["provider"] == "tavily"
    assert sources[0].metadata["published_date"] == "2026-06-04"


@pytest.mark.asyncio
async def test_web_search_requires_api_key() -> None:
    service, _ = make_service(api_key="")

    with pytest.raises(WebSearchConfigurationError):
        await service.search("query")


@pytest.mark.asyncio
async def test_web_search_caps_requested_results_by_setting() -> None:
    service, client = make_service()

    await service.search("query", max_results=10)

    assert client.calls[0]["max_results"] == 5
