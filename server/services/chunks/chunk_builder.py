from abc import ABC, abstractmethod

from schemas.rag import ChunkCreate, SegmentCreate
from services.chunks.context_chunk_planning_service import ContextChunkPlanGroup


class TranscriptChunkBuilder(ABC):
    # 저장된 STT segment를 강의 요약자료 생성에 사용할 맥락 단위 chunk로 변환합니다.
    @abstractmethod
    def build(self, segments: list[SegmentCreate]) -> list[ChunkCreate]:
        raise NotImplementedError


class ContextPlannedChunkBuilder:
    # LLM planner가 만든 segment range plan을 강의 전용 chunks 테이블 입력 모델로 변환합니다.
    # 기능 흐름:
    #   1. segment_index 기준으로 실제 segment를 찾는다
    #   2. plan group의 range가 유효한지 확인하며 ChunkCreate로 변환한다
    #   3. planning_method에 따라 lecture_context_plan_v1 또는 lecture_context_fallback_v1을 사용한다
    def build(
        self,
        segments: list[SegmentCreate],
        plan_groups: list[ContextChunkPlanGroup],
    ) -> list[ChunkCreate]:
        if not segments or not plan_groups:
            return []

        ordered_segments = self._ordered_non_empty_segments(segments)
        segment_by_index = {segment.segment_index: segment for segment in ordered_segments}
        chunks: list[ChunkCreate] = []

        for group in plan_groups:
            group_segments: list[SegmentCreate] = []
            for index in range(group.segment_start_index, group.segment_end_index + 1):
                if index not in segment_by_index:
                    raise ValueError("Context chunk plan references unknown segment.")
                group_segments.append(segment_by_index[index])
            if not group_segments:
                raise ValueError("Context chunk plan references no persisted segments.")
            chunks.append(self._to_chunk(len(chunks), group, group_segments))

        return chunks

    # segment 순서를 보존하고 빈 STT 조각을 제외합니다.
    def _ordered_non_empty_segments(
        self,
        segments: list[SegmentCreate],
    ) -> list[SegmentCreate]:
        return sorted(
            [segment for segment in segments if segment.text.strip()],
            key=lambda segment: segment.segment_index,
        )

    # 하나의 plan group과 해당 segment range를 실제 chunks 테이블 입력 모델로 변환합니다.
    # 파라미터:
    #   chunk_index: 저장 순서
    #   group: planner/fallback이 정한 segment range와 설명
    #   segments: group range에 포함되는 실제 SegmentCreate 목록
    def _to_chunk(
        self,
        chunk_index: int,
        group: ContextChunkPlanGroup,
        segments: list[SegmentCreate],
    ) -> ChunkCreate:
        metadata = {
            "segment_count": len(segments),
            "chunk_goal": "summary_context_lecture",
            "planning_method": group.planning_method,
            "planning_reason": group.reason,
        }
        if group.topic:
            metadata["planning_topic"] = group.topic
        if group.summary_hint:
            metadata["summary_hint"] = group.summary_hint
        if group.planning_error:
            metadata["planning_error"] = group.planning_error

        return ChunkCreate(
            chunk_index=chunk_index,
            chunk_strategy=self._chunk_strategy(group.planning_method),
            text=self._join_text(segments),
            segment_start_index=segments[0].segment_index,
            segment_end_index=segments[-1].segment_index,
            start_seconds=segments[0].start_seconds,
            end_seconds=segments[-1].end_seconds,
            speaker_labels=self._speaker_labels(segments),
            metadata=metadata,
            source_type=self._source_type(segments),
            source_page_start=self._min_value(s.source_page_start for s in segments),
            source_page_end=self._max_value(s.source_page_end for s in segments),
            source_slide_start=self._min_value(s.source_slide_start for s in segments),
            source_slide_end=self._max_value(s.source_slide_end for s in segments),
            source_start_seconds=self._min_value(s.source_start_seconds for s in segments),
            source_end_seconds=self._max_value(s.source_end_seconds for s in segments),
        )

    def _chunk_strategy(self, planning_method: str) -> str:
        if planning_method == "fallback":
            return "lecture_context_fallback_v1"
        return "lecture_context_plan_v1"

    def _join_text(self, segments: list[SegmentCreate]) -> str:
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip())

    def _speaker_labels(self, segments: list[SegmentCreate]) -> list[str]:
        labels: list[str] = []
        for segment in segments:
            if segment.speaker_label and segment.speaker_label not in labels:
                labels.append(segment.speaker_label)
        return labels

    def _source_type(self, segments: list[SegmentCreate]) -> str | None:
        source_types = {segment.source_type for segment in segments if segment.source_type}
        if len(source_types) == 1:
            return next(iter(source_types))
        return None

    def _min_value(self, values) -> float | int | None:
        present = [value for value in values if value is not None]
        return min(present) if present else None

    def _max_value(self, values) -> float | int | None:
        present = [value for value in values if value is not None]
        return max(present) if present else None


class DeterministicFallbackChunkBuilder:
    def __init__(
        self,
        lecture_max_seconds: float = 300.0,
        planned_chunk_builder: ContextPlannedChunkBuilder | None = None,
    ) -> None:
        self._lecture_max_seconds = lecture_max_seconds
        self._planned_chunk_builder = planned_chunk_builder or ContextPlannedChunkBuilder()

    # LLM planner를 사용할 수 없을 때 transcript 저장을 유지하기 위한 시간 기준 fallback chunk를 생성합니다.
    # 기능 흐름:
    #   1. 비어 있지 않은 segment를 index 순으로 정렬한다
    #   2. lecture_max_seconds를 넘지 않는 연속 range plan을 만든다
    #   3. ContextPlannedChunkBuilder로 동일한 ChunkCreate 변환 경로를 사용한다
    def build(
        self,
        segments: list[SegmentCreate],
    ) -> list[ChunkCreate]:
        ordered_segments = self._ordered_non_empty_segments(segments)
        if not ordered_segments:
            return []
        plan_groups = self._build_plan_groups(ordered_segments)
        return self._planned_chunk_builder.build(ordered_segments, plan_groups)

    # 강의 맥락 chunk가 너무 길어지는 것을 막기 위한 안전 range plan을 만듭니다.
    def _build_plan_groups(
        self,
        segments: list[SegmentCreate],
    ) -> list[ContextChunkPlanGroup]:
        groups: list[ContextChunkPlanGroup] = []
        current_start = segments[0]
        current_end = segments[0]

        for segment in segments[1:]:
            projected_seconds = segment.end_seconds - current_start.start_seconds
            if projected_seconds > self._lecture_max_seconds:
                groups.append(self._to_plan_group(current_start, current_end))
                current_start = segment
            current_end = segment

        groups.append(self._to_plan_group(current_start, current_end))
        return groups

    def _to_plan_group(
        self,
        start_segment: SegmentCreate,
        end_segment: SegmentCreate,
    ) -> ContextChunkPlanGroup:
        return ContextChunkPlanGroup(
            segment_start_index=start_segment.segment_index,
            segment_end_index=end_segment.segment_index,
            topic=None,
            reason=f"deterministic fallback: {self._lecture_max_seconds:g}초 안전 기준",
            summary_hint=None,
            planning_method="fallback",
        )

    def _ordered_non_empty_segments(
        self,
        segments: list[SegmentCreate],
    ) -> list[SegmentCreate]:
        return sorted(
            [segment for segment in segments if segment.text.strip()],
            key=lambda segment: segment.segment_index,
        )
