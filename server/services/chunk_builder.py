from abc import ABC, abstractmethod
from math import ceil

from schemas.rag import ChunkCreate, DomainType, SegmentCreate
from services.context_chunk_planning_service import ContextChunkPlanGroup


class TranscriptChunkBuilder(ABC):
    # 저장된 STT segment를 요약자료 생성에 사용할 맥락 단위 chunk로 변환합니다.
    @abstractmethod
    def build(self, segments: list[SegmentCreate]) -> list[ChunkCreate]:
        raise NotImplementedError


class MeetingChunkBuilder(TranscriptChunkBuilder):
    def __init__(
        self,
        max_tokens: int = 900,
        max_seconds: float = 180.0,
    ) -> None:
        self._max_tokens = max_tokens
        self._max_seconds = max_seconds

    # 회의 segment를 요약자료 생성에 사용할 발화 흐름 단위 chunk로 묶습니다.
    def build(self, segments: list[SegmentCreate]) -> list[ChunkCreate]:
        ordered_segments = self._ordered_non_empty_segments(segments)
        chunks: list[ChunkCreate] = []
        current: list[SegmentCreate] = []

        for segment in ordered_segments:
            if not current:
                current.append(segment)
                continue

            if self._should_start_new_chunk(current, segment):
                chunks.append(self._to_chunk(len(chunks), current))
                current = [segment]
                continue

            current.append(segment)

        if current:
            chunks.append(self._to_chunk(len(chunks), current))

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

    # 현재 fallback 기준으로 새 회의 맥락 chunk를 시작할지 판단합니다.
    def _should_start_new_chunk(
        self,
        current: list[SegmentCreate],
        next_segment: SegmentCreate,
    ) -> bool:
        current_text = self._join_text(current)
        projected_text = self._join_text([*current, next_segment])
        current_speaker = current[-1].speaker_label
        next_speaker = next_segment.speaker_label
        speaker_changed = (
            current_speaker is not None
            and next_speaker is not None
            and current_speaker != next_speaker
        )
        projected_seconds = next_segment.end_seconds - current[0].start_seconds

        return (
            self._estimate_tokens(projected_text) > self._max_tokens
            or projected_seconds > self._max_seconds
            or (speaker_changed and current_text.strip())
        )

    # 원본 segment와 시간 범위를 유지한 요약 맥락 ChunkCreate를 생성합니다.
    def _to_chunk(
        self,
        chunk_index: int,
        segments: list[SegmentCreate],
    ) -> ChunkCreate:
        return ChunkCreate(
            domain_type="meeting",
            chunk_index=chunk_index,
            chunk_strategy="meeting_speaker_turn_v1",
            text=self._join_text(segments),
            segment_start_index=segments[0].segment_index,
            segment_end_index=segments[-1].segment_index,
            start_seconds=segments[0].start_seconds,
            end_seconds=segments[-1].end_seconds,
            speaker_labels=self._speaker_labels(segments),
            metadata={
                "segment_count": len(segments),
                "chunk_goal": "summary_context_meeting",
                "planning_method": "fallback",
                "planning_reason": "deterministic meeting fallback builder",
            },
        )

    def _join_text(self, segments: list[SegmentCreate]) -> str:
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip())

    def _speaker_labels(self, segments: list[SegmentCreate]) -> list[str]:
        labels: list[str] = []
        for segment in segments:
            if segment.speaker_label and segment.speaker_label not in labels:
                labels.append(segment.speaker_label)
        return labels

    def _estimate_tokens(self, text: str) -> int:
        return max(1, ceil(len(text) / 3))


class LectureChunkBuilder(TranscriptChunkBuilder):
    def __init__(
        self,
        max_tokens: int = 1800,
        overlap_segments: int = 2,
    ) -> None:
        self._max_tokens = max_tokens
        self._overlap_segments = overlap_segments

    # 강의 segment를 요약자료 생성에 사용할 개념 흐름 단위 chunk로 묶습니다.
    def build(self, segments: list[SegmentCreate]) -> list[ChunkCreate]:
        ordered_segments = self._ordered_non_empty_segments(segments)
        chunks: list[ChunkCreate] = []
        current: list[SegmentCreate] = []
        current_overlap_count = 0

        for segment in ordered_segments:
            if not current:
                current.append(segment)
                continue

            projected_text = self._join_text([*current, segment])
            if self._estimate_tokens(projected_text) > self._max_tokens:
                chunks.append(
                    self._to_chunk(
                        len(chunks),
                        current,
                        overlap_from_previous=current_overlap_count,
                    )
                )
                overlap = self._overlap_tail(current)
                current = [*overlap, segment]
                current_overlap_count = len(overlap)
                continue

            current.append(segment)

        if current:
            chunks.append(
                self._to_chunk(
                    len(chunks),
                    current,
                    overlap_from_previous=current_overlap_count,
                )
            )

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

    # 개념 흐름을 유지하기 위해 이전 chunk의 마지막 segment 일부를 다음 chunk에 재사용합니다.
    def _overlap_tail(self, segments: list[SegmentCreate]) -> list[SegmentCreate]:
        if self._overlap_segments <= 0:
            return []
        return segments[-self._overlap_segments :]

    # 원본 segment와 시간 범위를 유지한 요약 맥락 ChunkCreate를 생성합니다.
    def _to_chunk(
        self,
        chunk_index: int,
        segments: list[SegmentCreate],
        overlap_from_previous: int,
    ) -> ChunkCreate:
        return ChunkCreate(
            domain_type="lecture",
            chunk_index=chunk_index,
            chunk_strategy="lecture_context_section_v1",
            text=self._join_text(segments),
            segment_start_index=segments[0].segment_index,
            segment_end_index=segments[-1].segment_index,
            start_seconds=segments[0].start_seconds,
            end_seconds=segments[-1].end_seconds,
            speaker_labels=self._speaker_labels(segments),
            metadata={
                "segment_count": len(segments),
                "overlap_from_previous": overlap_from_previous,
                "chunk_goal": "summary_context_lecture",
                "planning_method": "fallback",
                "planning_reason": "deterministic lecture fallback builder",
            },
        )

    def _join_text(self, segments: list[SegmentCreate]) -> str:
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip())

    def _speaker_labels(self, segments: list[SegmentCreate]) -> list[str]:
        labels: list[str] = []
        for segment in segments:
            if segment.speaker_label and segment.speaker_label not in labels:
                labels.append(segment.speaker_label)
        return labels

    # 실제 tokenizer 없이 텍스트 길이 기반으로 token 수를 대략 추정합니다.
    def _estimate_tokens(self, text: str) -> int:
        return max(1, ceil(len(text) / 3))


class ContextPlannedChunkBuilder:
    # LLM planner가 만든 segment range plan을 DB에 저장할 ChunkCreate 목록으로 변환합니다.
    # domain_type에 따라 chunk_goal과 chunk_strategy를 분리하고, plan의 판단 이유를 metadata에 보존합니다.
    def build(
        self,
        domain_type: DomainType,
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
            chunks.append(self._to_chunk(domain_type, len(chunks), group, group_segments))

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
    def _to_chunk(
        self,
        domain_type: DomainType,
        chunk_index: int,
        group: ContextChunkPlanGroup,
        segments: list[SegmentCreate],
    ) -> ChunkCreate:
        metadata = {
            "segment_count": len(segments),
            "chunk_goal": self._chunk_goal(domain_type),
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
            domain_type=domain_type,
            chunk_index=chunk_index,
            chunk_strategy=self._chunk_strategy(domain_type, group.planning_method),
            text=self._join_text(segments),
            segment_start_index=segments[0].segment_index,
            segment_end_index=segments[-1].segment_index,
            start_seconds=segments[0].start_seconds,
            end_seconds=segments[-1].end_seconds,
            speaker_labels=self._speaker_labels(segments),
            metadata=metadata,
        )

    def _chunk_goal(self, domain_type: DomainType) -> str:
        if domain_type == "meeting":
            return "summary_context_meeting"
        return "summary_context_lecture"

    def _chunk_strategy(self, domain_type: DomainType, planning_method: str) -> str:
        if planning_method == "fallback":
            return f"{domain_type}_context_fallback_v1"
        return f"{domain_type}_context_plan_v1"

    def _join_text(self, segments: list[SegmentCreate]) -> str:
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip())

    def _speaker_labels(self, segments: list[SegmentCreate]) -> list[str]:
        labels: list[str] = []
        for segment in segments:
            if segment.speaker_label and segment.speaker_label not in labels:
                labels.append(segment.speaker_label)
        return labels


class DeterministicFallbackChunkBuilder:
    def __init__(
        self,
        meeting_max_seconds: float = 180.0,
        lecture_max_seconds: float = 300.0,
        planned_chunk_builder: ContextPlannedChunkBuilder | None = None,
    ) -> None:
        self._meeting_max_seconds = meeting_max_seconds
        self._lecture_max_seconds = lecture_max_seconds
        self._planned_chunk_builder = planned_chunk_builder or ContextPlannedChunkBuilder()

    # LLM planner를 사용할 수 없을 때 transcript 저장을 유지하기 위한 시간 기준 fallback chunk를 생성합니다.
    # 이 기준은 주된 맥락 판단이 아니라 과도하게 긴 chunk만 막는 안전장치입니다.
    def build(
        self,
        domain_type: DomainType,
        segments: list[SegmentCreate],
    ) -> list[ChunkCreate]:
        ordered_segments = self._ordered_non_empty_segments(segments)
        if not ordered_segments:
            return []
        plan_groups = self._build_plan_groups(domain_type, ordered_segments)
        return self._planned_chunk_builder.build(domain_type, ordered_segments, plan_groups)

    # meeting은 180초, lecture는 300초를 넘기지 않도록 연속 segment range plan을 만듭니다.
    def _build_plan_groups(
        self,
        domain_type: DomainType,
        segments: list[SegmentCreate],
    ) -> list[ContextChunkPlanGroup]:
        max_seconds = (
            self._meeting_max_seconds if domain_type == "meeting" else self._lecture_max_seconds
        )
        groups: list[ContextChunkPlanGroup] = []
        current_start = segments[0]
        current_end = segments[0]

        for segment in segments[1:]:
            projected_seconds = segment.end_seconds - current_start.start_seconds
            if projected_seconds > max_seconds:
                groups.append(self._to_plan_group(current_start, current_end, max_seconds))
                current_start = segment
            current_end = segment

        groups.append(self._to_plan_group(current_start, current_end, max_seconds))
        return groups

    def _to_plan_group(
        self,
        start_segment: SegmentCreate,
        end_segment: SegmentCreate,
        max_seconds: float,
    ) -> ContextChunkPlanGroup:
        return ContextChunkPlanGroup(
            segment_start_index=start_segment.segment_index,
            segment_end_index=end_segment.segment_index,
            topic=None,
            reason=f"deterministic fallback: {max_seconds:g}초 안전 기준",
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


# domain_type에 맞는 fallback chunk builder를 선택합니다.
def get_chunk_builder(domain_type: DomainType) -> TranscriptChunkBuilder:
    if domain_type == "meeting":
        return MeetingChunkBuilder()
    if domain_type == "lecture":
        return LectureChunkBuilder()
    raise ValueError(f"Unsupported domain_type: {domain_type}")
