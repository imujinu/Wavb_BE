from uuid import UUID, uuid4

from fastapi import APIRouter

from schemas.rag import (
    RagChatCompletedResponse,
    RagChatInterruptedResponse,
    RagChatRequest,
    RagChatResumeRequest,
    RagSource,
)


router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/chat", response_model=RagChatCompletedResponse | RagChatInterruptedResponse)
async def chat(request: RagChatRequest) -> dict:
    """
    RAG 챗봇 엔드포인트: 질문에 대한 답변과 근거를 반환

    기능 요약:
    - 사용자 질문을 받아 검색 대상(transcript_id 또는 user_id)을 검증하고
    - 검색 범위 내 문서 존재 여부를 확인해 초기 confidence를 계산하며
    - confidence가 낮으면 interrupt 응답을 반환해 사용자 재질의를 유도
    - confidence가 충분하면 thread를 생성하고 LangGraph 실행 (v1에서는 stub)

    기능 흐름:
    1. 요청 검증 (transcript_id 또는 user_id 필수) - Pydantic에서 자동 처리
    2. 검색 대상 문서 존재 여부 확인 (Step 3에서 구현)
    3. 초기 confidence 계산 (문서 hit count 기반)
    4. confidence < RAG_MIN_CONFIDENCE이면 interrupt 응답
    5. 그 외 경우 completed 응답 (v1에서는 stub 답변)

    파라미터:
    - query: 사용자 질문 (1-1000자)
    - transcript_id: 단일 transcript 검색 대상 UUID (선택, user_id와 함께 사용 가능)
    - user_id: 사용자의 모든 transcript 검색 대상 UUID (선택, transcript_id와 함께 사용 가능)
    - domain_type: 검색 필터 (meeting|lecture, 선택)
    - conversation_id: 대화 세션 ID (선택)
    """
    # 1. 검색 범위 검증: request.py의 pydantic model_validator에서 처리됨
    transcript_id = request.transcript_id
    user_id = request.user_id

    # 2. 검색 가능한 문서 존재 여부 확인
    # TODO: Step 3에서 search_chunks_by_keyword, search_chunks_by_vector, search_chunks_hybrid 메서드 구현
    # 현재는 검색 결과 수를 0으로 설정 (stub)
    search_result_count = 0

    # 3. 초기 confidence 계산: 검색 결과 수 기반 휴리스틱
    # (실제 confidence는 LangGraph에서 rerank 후 계산하지만, v1에서는 단순 휴리스틱)
    initial_confidence = min(search_result_count / 5.0, 1.0)  # 5개 이상이면 1.0

    # 4. confidence가 낮으면 interrupt 반환
    if initial_confidence < 0.35:  # RAG_MIN_CONFIDENCE 값과 동일
        return RagChatInterruptedResponse(
            status="interrupted",
            thread_id=uuid4(),
            reason="insufficient_context",
            message="검색 범위 내 관련 자료가 부족합니다. 질문을 조금 더 구체화해 주세요.",
            suggested_queries=[
                f"'{request.query}'에 대한 더 구체적인 내용",
                f"'{request.query}'와 관련된 사례 또는 예시",
            ],
        ).model_dump()

    # 5. confidence가 충분한 경우 completed 응답 반환 (v1 stub)
    # TODO: LangGraph 통합 시 실제 답변 생성 및 sources 수집 로직 구현
    sources = [
        RagSource(
            transcript_id=transcript_id or user_id,
            parent_chunk_id=UUID("00000000-0000-0000-0000-000000000000"),
            child_index=0,
            start_seconds=None,
            end_seconds=None,
            score=0.85,
            snippet="검색 결과 샘플 (v1 stub)",
        )
    ]

    return RagChatCompletedResponse(
        status="completed",
        answer="현재는 준비 중입니다. LangGraph 통합 후 실제 답변이 제공됩니다.",
        confidence=initial_confidence,
        sources=sources,
    ).model_dump()


@router.post("/chat/resume", response_model=RagChatCompletedResponse | RagChatInterruptedResponse)
async def resume_chat(request: RagChatResumeRequest) -> dict:
    """
    RAG 챗봇 재개 엔드포인트: interrupted 상태 후 사용자 수정 입력으로 재개

    기능 요약:
    - interrupt 상태의 대화를 thread_id로 복구하고
    - 사용자가 수정한 query를 입력받아
    - LangGraph의 search_router부터 재개 (v1에서는 stub)

    기능 흐름:
    1. thread_id 유효성 확인 (LangGraph checkpointer에서 상태 복구) - Step 4에서 구현
    2. 수정된 query로 재검색 (checkpoint 상태 복구 후 search_router 재개)
    3. completed 또는 다시 interrupted 응답

    파라미터:
    - thread_id: interrupt 응답에서 받은 thread UUID
    - query: 사용자가 수정한 질문 (1-1000자)
    """
    # 1. thread_id 유효성 확인 (Step 4에서 LangGraph checkpointer 통합 시 구현)
    # TODO: LangGraph checkpoint에서 thread_id로 상태 복구

    # 2. 재개 로직 (v1 stub)
    # TODO: checkpoint 상태 복구 후 search_router부터 query(request.query) 재검색

    # 3. 임시 completed 응답 반환
    return RagChatCompletedResponse(
        status="completed",
        answer=f"재개된 대화입니다 (thread: {request.thread_id}, query: '{request.query}'). LangGraph 통합 후 실제 처리가 수행됩니다.",
        confidence=0.5,
        sources=[],
    ).model_dump()
