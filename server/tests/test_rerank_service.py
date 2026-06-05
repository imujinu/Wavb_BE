import pytest

from schemas.rag import RetrievedSource
from services.rag.rerank_service import IdentityRerankService


@pytest.mark.asyncio
async def test_identity_rerank_service_preserves_order() -> None:
    sources = [
        RetrievedSource(source_type="web", title="B", snippet="second"),
        RetrievedSource(source_type="document", title="A", snippet="first"),
    ]

    result = await IdentityRerankService().rerank("query", sources)

    assert result == sources
