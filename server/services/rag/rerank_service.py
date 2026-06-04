from typing import Protocol

from schemas.rag import RetrievedSource


class RerankService(Protocol):
    async def rerank(
        self,
        query: str,
        sources: list[RetrievedSource],
    ) -> list[RetrievedSource]:
        ...


class IdentityRerankService:
    # 기능 요약: reranker 교체 지점을 만들고 v1에서는 입력 순서를 그대로 유지한다.
    async def rerank(
        self,
        query: str,
        sources: list[RetrievedSource],
    ) -> list[RetrievedSource]:
        return sources
