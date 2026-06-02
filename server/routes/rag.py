# RAG 검색 + 응답 생성 파이프라인의 HTTP 진입점.
# POST /rag/query 단일 엔드포인트로 retrieval(RagQueryService) + generation(RagResponseService)을
# 순차 조율하여 자연어 답변과 근거 청크를 함께 반환한다.
# 외부에서 RAG 파이프라인을 호출할 유일한 통로이므로 라우터 등록 없이는 기능이 노출되지 않는다.

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends

from db.connection import DatabaseConnection, get_connection
from repositories.rag_repository import RagRepository
from schemas.rag import RagQueryRequest, RagQueryResponse
from services.rag.embedding_service import EmbeddingService
from services.rag.morpheme_service import MorphemeService
from services.rag.rag_query_service import RagQueryService
from services.rag.rag_response_service import RagResponseService


router = APIRouter(prefix="/rag", tags=["rag"])


# RagQueryService 요청 범위 인스턴스를 생성하는 의존성.
# 동작 흐름:
#   1. 요청 범위 DB 커넥션을 RagRepository에 주입
#   2. 형태소 분석(MorphemeService) + 임베딩(EmbeddingService) 서비스 생성
#   3. 세 의존성을 RagQueryService에 주입하여 반환
# 필요성: 라우터에서 직접 서비스를 조립하면 의존성 결합이 커지므로 DI 함수로 분리한다.
async def get_rag_query_service(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[RagQueryService]:
    # 1. DB 커넥션을 RagRepository에 주입 (요청 범위)
    repository = RagRepository(connection)
    # 2. 검색 전처리용 서비스 생성 — query 형태소 분석 + 원문 임베딩
    embedding_service = EmbeddingService()
    morpheme_service = MorphemeService()
    # 3. retrieval 조율 서비스로 묶어 반환
    yield RagQueryService(
        repository=repository,
        embedding_service=embedding_service,
        morpheme_service=morpheme_service,
    )


# RagResponseService 인스턴스를 생성하는 의존성.
# 동작 흐름: OpenAI client + 모델 설정을 캡슐화한 생성 서비스를 반환한다.
# 필요성: generation 단계는 DB 의존성이 없으므로 단순 생성만 담당하는 별도 DI 함수로 분리한다.
def get_rag_response_service() -> RagResponseService:
    return RagResponseService()


@router.post("/query", response_model=RagQueryResponse)
async def rag_query(
    request: RagQueryRequest,
    rag_query_service: RagQueryService = Depends(get_rag_query_service),
    rag_response_service: RagResponseService = Depends(get_rag_response_service),
) -> RagQueryResponse:
    """
    기능 요약: 자연어 질의를 하이브리드 검색 → LLM 답변 생성 순으로 처리해 응답한다.

    기능 흐름:
        1. RagQueryService.search(...) → 하이브리드 검색 + parent hydration으로 근거 청크 조회
        2. RagResponseService.generate(query, parent_chunks) → 청크 범위 내 한국어 답변 생성
        3. answer + sources + chunks_retrieved를 RagQueryResponse로 묶어 반환

    파라미터:
        request: query(질의), transcript_id(범위 한정), user_id(임시), top_k(반환 수)를 담은 요청 모델
    """
    # 1. retrieval — 형태소/임베딩 전처리 후 하이브리드 검색으로 근거 청크 확보
    parent_chunks = await rag_query_service.search(
        query=request.query,
        transcript_id=request.transcript_id,
        user_id=request.user_id,
        top_k=request.top_k,
    )

    # 2. generation — 검색된 청크를 context로 LLM 답변 생성
    answer = await rag_response_service.generate(request.query, parent_chunks)

    # 3. 답변 + 근거 청크 + 검색 청크 수를 응답으로 반환
    return RagQueryResponse(
        answer=answer,
        sources=parent_chunks,
        chunks_retrieved=len(parent_chunks),
    )
