# 사용자 자연어 질의를 받아 search_chunks에 대한 하이브리드 검색을 수행하고,
# 검색 히트된 child chunk의 parent chunk 전체 문맥을 반환하는 서비스.
# RAG 파이프라인의 retrieval 단계를 담당하며, LLM 응답 생성(G) 단계로 넘어가기 전
# query 전처리(형태소 분석/임베딩) + hybrid search + parent hydration을 조율한다.

from uuid import UUID

from repositories.rag_repository import RagRepository
from schemas.rag import ParentChunkResult, SearchChunkHit
from services.rag.embedding_service import EmbeddingService
from services.rag.morpheme_service import MorphemeService


class RagQueryService:
    def __init__(
        self,
        repository: RagRepository,
        embedding_service: EmbeddingService,
        morpheme_service: MorphemeService,
    ) -> None:
        # 의존성 주입 — 테스트 시 각각 fake/mock으로 교체 가능하도록 외부에서 주입
        self._repository = repository
        self._embedding_service = embedding_service
        self._morpheme_service = morpheme_service

    async def search(
        self,
        query: str,
        transcript_id: UUID | None,
        user_id: UUID | None,
        top_k: int,
    ) -> list[ParentChunkResult]:
        """
        기능 요약: 자연어 query를 받아 하이브리드 검색을 수행하고 parent chunk 목록을 반환한다.

        기능 흐름:
            1. MorphemeService.tokenize(query) → morpheme_query (FTS용 형태소 추출 텍스트)
            2. EmbeddingService.embed([query]) → query_embedding (원문 그대로 임베딩)
               - 원문 사용 이유: 형태소 분석 결과("일정 논의")보다 원문("다음 일정 논의했던 내용")이
                 의미 벡터 공간에서 더 정확한 위치를 가진다. FTS는 morpheme_query, 벡터는 원문을
                 사용해 각 방식의 장점을 최대화한다.
            3. RagRepository.search_chunks_hybrid(...) → list[SearchChunkHit] (score 내림차순)
            4. score 순서를 유지한 채 unique parent_chunk_id 목록 추출
            5. RagRepository.get_parent_chunks(parent_chunk_ids) → list[ParentChunkResult]
            6. parent chunk 목록을 score 순으로 재정렬하여 반환

        파라미터:
            query: 사용자 자연어 질의 (예: "다음 출시 일정 논의했던 내용")
            transcript_id: 검색 범위 한정용 transcript UUID (None이면 전체 transcript 대상)
            user_id: 검색 범위 한정용 user UUID (None이면 user 필터 미적용)
            top_k: 반환할 최대 child hit 수 (예: 5)

        반환:
            score 내림차순으로 정렬된 ParentChunkResult 목록 (중복 제거됨)
        """
        # 1. 형태소 분석 — 조사/어미를 제거하여 FTS 키워드 매칭 정확도 향상
        #    예: "다음 출시 일정을 논의했다" → "다음 출시 일정 논의"
        morpheme_query = self._morpheme_service.tokenize(query)

        # 2. 임베딩 생성 — 원문 그대로 사용하여 의미 벡터 공간의 정확도 보존
        #    EmbeddingService.embed는 batch 인터페이스이므로 단일 query를 list로 감싸 호출
        embeddings = await self._embedding_service.embed([query])
        query_embedding = embeddings[0]

        # 3. 하이브리드 검색 실행 — keyword(0.6) + vector(0.4) 가중 합산 점수로 top_k 반환
        hits: list[SearchChunkHit] = await self._repository.search_chunks_hybrid(
            morpheme_query=morpheme_query,
            embedding=query_embedding,
            transcript_id=transcript_id,
            user_id=user_id,
            top_k=top_k,
        )

        # 4. 검색 결과 없음 — 빈 목록 조기 반환
        if not hits:
            return []

        # 5. score 순서를 유지한 채 unique parent_chunk_id 추출
        #    동일 parent의 여러 child가 hit된 경우 가장 높은 score child를 기준으로 한 번만 포함
        ordered_parent_ids = self._extract_unique_parent_ids(hits)

        # 6. parent chunk 일괄 조회 — ANY($1::uuid[])로 N+1 쿼리 방지
        
        parent_chunks = await self._repository.get_parent_chunks(ordered_parent_ids)

        # 7. DB 반환 순서는 보장되지 않으므로 score 순서로 재정렬하여 반환
        return self._sort_by_search_order(parent_chunks, ordered_parent_ids)

    # search_chunks_hybrid가 반환한 score 순 hit 목록에서
    # 동일 parent_chunk_id 중복을 제거하면서 첫 등장 순서를 유지한 UUID 리스트를 만든다.
    # dict의 insertion order 보장 특성을 이용해 O(N)으로 처리한다.
    def _extract_unique_parent_ids(
        self,
        hits: list[SearchChunkHit],
    ) -> list[UUID]:
        # dict.fromkeys로 unique 추출 — Python 3.7+ insertion order 보장

        unique_ids: dict[UUID, None] = {}
        for hit in hits:
            unique_ids.setdefault(hit.parent_chunk_id, None)
        return list(unique_ids.keys())

    # get_parent_chunks가 반환한 ParentChunkResult 목록을
    # 검색 score 순서(= ordered_parent_ids 순서)에 맞춰 재정렬한다.
    # DB의 ANY 조회 결과 순서가 보장되지 않으므로 명시적 정렬이 필요하다.
    def _sort_by_search_order(
        self,
        parent_chunks: list[ParentChunkResult],
        ordered_parent_ids: list[UUID],
    ) -> list[ParentChunkResult]:
        # parent_chunk_id → ParentChunkResult 매핑으로 O(1) 조회
        by_id: dict[UUID, ParentChunkResult] = {chunk.id: chunk for chunk in parent_chunks}

        # ordered_parent_ids 순서를 따르되, 조회 결과에 없는 id는 건너뛴다
        # (drop 되는 경우는 실 서비스에서 거의 없지만, race condition으로 parent가 삭제된 경우 대비)
        return [by_id[pid] for pid in ordered_parent_ids if pid in by_id]
