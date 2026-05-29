# parent context chunk를 vector search에 최적화된 child search unit으로 분할한다.
# chunks 테이블의 row를 받아 내부 segment 범위를 adaptive grouping해서 search_chunks 생성 입력값을 만든다.
# MorphemeService가 주입된 경우 각 child unit의 text_morphemes를 생성하여 FTS 성능을 향상시킨다.

from __future__ import annotations

from schemas.rag import ChunkRow, SearchChunkCreate, SegmentCreate

# 순환 임포트 방지를 위해 타입 힌팅 전용 임포트
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.morpheme_service import MorphemeService


class SearchChunkBuilder:

    def __init__(
        self,
        max_segments: int = 4,
        max_chars: int = 800,
        max_seconds: float = 90.0,
        morpheme_service: MorphemeService | None = None,
    ) -> None:
        self._max_segments = max_segments
        self._max_chars = max_chars
        self._max_seconds = max_seconds
        # MorphemeService 주입 — None이면 text_morphemes를 생성하지 않고 FTS는 원문 fallback
        self._morpheme_service = morpheme_service
    # 입력으로 parent chunks와 segments를 받아 각 chunk의 segment 범위에 속하는 segments를 adaptive grouping해서 child search unit 리스트로 반환한다.
    def build(
        self,
        chunks: list[ChunkRow],
        segments: list[SegmentCreate],
    ) -> list[SearchChunkCreate]:
  
        # 1. segment_index 기반 빠른 조회 딕셔너리 구성
        segment_by_index: dict[int, SegmentCreate] = {
            seg.segment_index: seg for seg in segments
        }

        all_children: list[SearchChunkCreate] = []

        for chunk in chunks:
            # 2. parent chunk 범위에 속하는 segments 추출
            segments_in_range = self._get_segments_in_range(chunk, segment_by_index)

            # 3. adaptive grouping으로 child search unit 생성
            children = self._build_children_for_chunk(chunk, segments_in_range)

            # 4. 결과 누적
            all_children.extend(children)

        return all_children

    def _build_children_for_chunk(
        self,
        chunk: ChunkRow,
        segments_in_range: list[SegmentCreate],
    ) -> list[SearchChunkCreate]:
  
        # segment_index 기준 오름차순 정렬
        ordered = sorted(segments_in_range, key=lambda s: s.segment_index)

        children: list[SearchChunkCreate] = []
        # child_index는 parent chunk마다 0부터 다시 시작
        child_index = 0
        current: list[SegmentCreate] = []

        for segment in ordered:
            if not current:
                # 1. 첫 번째 segment는 무조건 버퍼에 추가
                current.append(segment)
                continue

            projected_text_len = len(self._join_text([*current, segment]))
            projected_seconds = segment.end_seconds - current[0].start_seconds
            at_max = len(current) >= self._max_segments

            if projected_text_len > self._max_chars or projected_seconds > self._max_seconds or at_max:
                # 2. 초과 조건 충족 — 현재 버퍼를 child로 확정
                children.append(self._to_search_chunk(chunk, child_index, current))
                child_index += 1
                current = [segment]
            else:
                # 3. 아직 여유 있음 — 버퍼에 추가
                current.append(segment)

        # 4. 마지막 잔여 버퍼 처리
        if current:
            children.append(self._to_search_chunk(chunk, child_index, current))

        return children

    # ChunkRow의 segment 범위에 속하는 segments만 필터링해 반환한다.
    # segment_start_index 또는 segment_end_index가 None이면 빈 리스트를 반환해 안전하게 처리한다.
    def _get_segments_in_range(
        self,
        chunk: ChunkRow,
        segment_by_index: dict[int, SegmentCreate],
    ) -> list[SegmentCreate]:
        if chunk.segment_start_index is None or chunk.segment_end_index is None:
            return []

        return [
            segment_by_index[idx]
            for idx in range(chunk.segment_start_index, chunk.segment_end_index + 1)
            if idx in segment_by_index
        ]

    # segments 리스트를 SearchChunkCreate로 변환한다.
    # parent chunk 정보와 child_index를 함께 기록해 계층 관계를 보존한다.
    # MorphemeService가 주입된 경우 text_morphemes를 생성하여 FTS 정확도를 향상시킨다.
    def _to_search_chunk(
        self,
        chunk: ChunkRow,
        child_index: int,
        segments: list[SegmentCreate],
    ) -> SearchChunkCreate:
        # 1. segment 텍스트 결합
        joined_text = self._join_text(segments)

        # 2. MorphemeService가 주입된 경우 형태소 분석 수행, 없으면 None (FTS에서 원문 fallback)
        text_morphemes: str | None = None
        if self._morpheme_service is not None:
            text_morphemes = self._morpheme_service.tokenize(joined_text)

        return SearchChunkCreate(
            parent_chunk_id=chunk.id,
            child_index=child_index,
            segment_start_index=segments[0].segment_index,
            segment_end_index=segments[-1].segment_index,
            start_seconds=segments[0].start_seconds,
            end_seconds=segments[-1].end_seconds,
            text=joined_text,
            text_morphemes=text_morphemes,
            metadata={
                "parent_chunk_index": chunk.chunk_index,
                "child_goal": "vector_search",
                "grouping_strategy": "adaptive_segments_v1",
                "segment_count": len(segments),
            },
        )

    # segments의 text.strip()을 공백으로 합쳐 단일 문자열로 반환한다.
    def _join_text(self, segments: list[SegmentCreate]) -> str:
        return " ".join(seg.text.strip() for seg in segments if seg.text.strip())
