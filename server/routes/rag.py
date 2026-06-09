from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status

from db.connection import DatabaseConnection, get_connection
from dependencies.auth import get_current_user
from repositories.rag_repository import RagRepository
from schemas.auth import CurrentUser
from schemas.rag import RagQueryRequest, RagQueryResponse, RetrievedSource
from services.rag.embedding_service import EmbeddingService
from services.rag.morpheme_service import MorphemeService
from services.rag.rag_query_service import RagQueryService
from services.rag.rag_response_service import RagResponseService
from services.rag.rerank_service import IdentityRerankService, RerankService
from services.rag.web_search_service import (
    WebSearchConfigurationError,
    WebSearchProviderError,
    WebSearchService,
)


router = APIRouter(prefix="/rag", tags=["rag"])


async def get_rag_query_service(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[RagQueryService]:
    repository = RagRepository(connection)
    embedding_service = EmbeddingService()
    morpheme_service = MorphemeService()
    yield RagQueryService(
        repository=repository,
        embedding_service=embedding_service,
        morpheme_service=morpheme_service,
    )


def get_rag_response_service() -> RagResponseService:
    return RagResponseService()


def get_web_search_service() -> WebSearchService:
    return WebSearchService()


def get_rerank_service() -> RerankService:
    return IdentityRerankService()


@router.post(
    "/query",
    response_model=RagQueryResponse,
    summary="문서, 웹 또는 하이브리드 범위에서 RAG 검색을 수행하고 답변을 생성한다.",
)
async def rag_query(
    request: RagQueryRequest,
    current_user: CurrentUser = Depends(get_current_user),
    rag_query_service: RagQueryService = Depends(get_rag_query_service),
    rag_response_service: RagResponseService = Depends(get_rag_response_service),
    web_search_service: WebSearchService = Depends(get_web_search_service),
    rerank_service: RerankService = Depends(get_rerank_service),
) -> RagQueryResponse:
    """문서, 웹, 하이브리드 scope에 따라 RAG source를 모으고 답변을 생성한다."""
    warnings: list[str] = []
    document_sources: list[RetrievedSource] = []
    web_sources: list[RetrievedSource] = []

    if request.scope in {"document", "hybrid"}:
        document_sources = await rag_query_service.search(
            query=request.query,
            transcript_ids=request.transcript_ids,
            user_id=current_user.user_id,
            top_k=request.top_k,
        )

    if request.scope in {"web", "hybrid"}:
        try:
            web_sources = await web_search_service.search(
                request.query,
                max_results=request.top_k,
            )
        except (WebSearchConfigurationError, WebSearchProviderError) as exc:
            if request.scope == "hybrid" and document_sources:
                warnings.append(str(exc))
            else:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=str(exc),
                ) from exc

    sources = _select_sources(
        scope=request.scope,
        document_sources=document_sources,
        web_sources=web_sources,
        top_k=request.top_k,
    )
    sources = await rerank_service.rerank(request.query, sources)
    sources = sources[: request.top_k]

    answer = await rag_response_service.generate(request.query, sources)
    return RagQueryResponse(
        answer=answer,
        sources=sources,
        warnings=warnings,
    )


def _select_sources(
    scope: str,
    document_sources: list[RetrievedSource],
    web_sources: list[RetrievedSource],
    top_k: int,
) -> list[RetrievedSource]:
    """scope별 후보 source를 top_k 안으로 정리한다."""
    if scope == "document":
        return document_sources[:top_k]
    if scope == "web":
        return _normalize_web_scores(web_sources)[:top_k]

    normalized_documents = _normalize_document_scores(document_sources)
    normalized_web = _normalize_web_scores(web_sources)
    ranked = (
        [(0, source) for source in normalized_documents]
        + [(1, source) for source in normalized_web]
    )
    ranked.sort(key=lambda item: (-(item[1].score or 0.0), item[0]))
    return [source for _, source in ranked[:top_k]]


def _normalize_document_scores(
    sources: list[RetrievedSource],
) -> list[RetrievedSource]:
    """문서 RRF score를 source 목록 내부 기준 0~1 범위로 정규화한다."""
    max_score = max((source.score or 0.0 for source in sources), default=0.0)
    if max_score <= 0:
        return [source.model_copy(update={"score": 0.0}) for source in sources]
    return [
        source.model_copy(update={"score": (source.score or 0.0) / max_score})
        for source in sources
    ]


def _normalize_web_scores(
    sources: list[RetrievedSource],
) -> list[RetrievedSource]:
    """Tavily score를 0~1 범위로 보정한다."""
    return [
        source.model_copy(update={"score": _clamp_score(source.score)})
        for source in sources
    ]


def _clamp_score(score: float | None) -> float:
    if score is None:
        return 0.0
    return max(0.0, min(1.0, score))
