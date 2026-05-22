from abc import ABC, abstractmethod
from math import ceil

from schemas.rag import ChunkCreate, DomainType, SegmentCreate


class TranscriptChunkBuilder(ABC):
    # Convert persisted STT segments into retrieval chunks for one transcript domain.
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

    # Build smaller factual chunks that preserve speaker and timestamp evidence.
    # 빌드 전략: 발화자 변경, 긴 시간 간격, 또는 큰 텍스트 증가가 있을 때 새 청크 시작. 
    # 이렇게 하면 검색된 청크가 발화자와 시간 범위에 대한 명확한 증거를 유지하면서도 너무 크지 않도록 합니다.
    def build(self, segments: list[SegmentCreate]) -> list[ChunkCreate]:
        # 텍스트가 있는 세그먼트만 포함
        ordered_segments = self._ordered_non_empty_segments(segments)

        chunks: list[ChunkCreate] = []
        current: list[SegmentCreate] = []

        for segment in ordered_segments:
            # 청크가 비어 있다면 바로 추가
            if not current:
                current.append(segment)
                continue
            
            # 청크를 새로 시작해야 하는지 판단
            if self._should_start_new_chunk(current, segment):
                chunks.append(self._to_chunk(len(chunks), current))
                current = [segment]
                continue

            current.append(segment)

        if current:
            chunks.append(self._to_chunk(len(chunks), current))

        return chunks

    # Keep segments ordered and skip blank STT fragments before building chunks.
    # 세그먼트 순서를 유지하고 빈 STT 조각을 건너뛰어 청크를 빌드하기 전에 텍스트가 있는 세그먼트만 포함합니다.
    def _ordered_non_empty_segments(
        self,
        segments: list[SegmentCreate],
    ) -> list[SegmentCreate]:
        return sorted(
            [segment for segment in segments if segment.text.strip()],
            key=lambda segment: segment.segment_index,
        )

    # Start a new meeting chunk at speaker changes, long time spans, or large text.
    # 발화자 변경, 긴 시간 간격 또는 큰 텍스트 증가가 있을 때 새 회의 청크를 시작합니다.
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

    # Create a ChunkCreate model while preserving source segment and time ranges.
    # 원본 세그먼트 및 시간 범위를 유지하면서 ChunkCreate 모델을 생성합니다.
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
                "chunk_goal": "factual_retrieval",
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

    # Build larger context chunks with light segment overlap for concept continuity.
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
                # 개념 연속성을 위해 다음 청크에서 마지막 몇 개 세그먼트를 재사용합니다.
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

    # Keep segments ordered and skip blank STT fragments before building chunks.
    # 세그먼트 순서를 유지하고 빈 STT 조각을 건너뛰어 청크를 빌드하기 전에 텍스트가 있는 세그먼트만 포함합니다.
    def _ordered_non_empty_segments(
        self,
        segments: list[SegmentCreate],
    ) -> list[SegmentCreate]:
        return sorted(
            [segment for segment in segments if segment.text.strip()],
            key=lambda segment: segment.segment_index,
        )

    # Reuse the last few lecture segments in the next chunk to retain context. 
    def _overlap_tail(self, segments: list[SegmentCreate]) -> list[SegmentCreate]:
        if self._overlap_segments <= 0:
            return []
        return segments[-self._overlap_segments :]

    # Create a ChunkCreate model while preserving source segment and time ranges.
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
                "chunk_goal": "conceptual_retrieval",
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

    # 토큰 수를 대략적으로 추정하는 방법입니다. 실제 토큰 수는 사용하는 토크나이저에 따라 다를 수 있지만, 일반적으로 영어 텍스트의 경우 1 토큰이 약 3 문자라고 가정할 수 있습니다. 따라서 텍스트 길이를 3으로 나누고 올림하여 토큰 수를 추정합니다. 최소값을 1로 설정하여 빈 텍스트에 대해서도 1 토큰으로 처리합니다.
    def _estimate_tokens(self, text: str) -> int:
        return max(1, ceil(len(text) / 3))


# Select the domain-specific chunking strategy without leaking that choice to callers.
def get_chunk_builder(domain_type: DomainType) -> TranscriptChunkBuilder:
    if domain_type == "meeting":
        return MeetingChunkBuilder()
    if domain_type == "lecture":
        return LectureChunkBuilder()
    raise ValueError(f"Unsupported domain_type: {domain_type}")
