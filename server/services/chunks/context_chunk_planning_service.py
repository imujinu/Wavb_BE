import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI

from schemas.rag import SegmentCreate
from settings import get_settings


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "You plan context chunks for Korean voice transcripts. "
    "Return only valid JSON. Do not summarize beyond the given segments."
)


@dataclass(frozen=True)
class ContextChunkPlanGroup:
    segment_start_index: int
    segment_end_index: int
    topic: str | None
    reason: str
    summary_hint: str | None
    planning_method: str = "llm"
    planning_error: str | None = None


class ContextChunkPlanningService:
    def __init__(
        self,
        lecture_fallback_max_seconds: float = 300.0,
    ) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OPENAI_API_KEY is not configured.",
            )
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_summary_model
        self._lecture_fallback_max_seconds = lecture_fallback_max_seconds

    # segment 목록을 LLM에 전달해 요약자료용 맥락 chunk plan을 생성합니다.
    # LLM 호출 또는 응답 검증에 실패하면 시간 기준 fallback plan을 반환합니다.
    async def plan_chunks(
        self,
        segments: list[SegmentCreate],
    ) -> list[ContextChunkPlanGroup]:
        ordered_segments = self._ordered_non_empty_segments(segments)
        if not ordered_segments:
            return []

        try:
            response_content = await self._request_plan(ordered_segments)
            groups = self._parse_plan(response_content)
            self._validate_plan(groups, ordered_segments)
            return groups
        except Exception as exc:
            logger.exception(
                "Context chunk planning failed; falling back to deterministic plan."
            )
            return self._fallback_plan(
                ordered_segments,
                error_message=f"{type(exc).__name__}: {exc}",
            )

    # OpenAI chat completion을 호출해 연속 segment range 기반 chunk plan JSON을 요청합니다.
    # 강의의 개념/소주제 흐름을 기준으로 맥락 경계를 결정하도록 요청합니다.
    async def _request_plan(
        self,
        segments: list[SegmentCreate],
    ) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self._build_prompt(segments)},
                ],
            )
        except APIError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Context chunk planning provider failed.",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Context chunk planning failed.",
            ) from exc

        content = response.choices[0].message.content
        if not content or not content.strip():
            raise ValueError("Context chunk planning provider returned an empty response.")
        return content

    # 맥락 단위 chunk boundary만 결정하도록 segment 목록과 도메인별 기준을 프롬프트로 구성합니다.
    # segment index는 누락이나 중복 없이 연속 range로 반환하도록 요구합니다.
    def _build_prompt(
        self,
        segments: list[SegmentCreate],
    ) -> str:
        domain_instruction = (
            "강의 transcript입니다. 하나의 개념, 소주제, 정의-예시-결론 흐름 단위로 "
            "연속 segment를 묶으세요. 예시는 해당 개념 chunk에 포함하고 새 개념으로 "
            "넘어갈 때 나누세요."
        )

        segment_lines = "\n".join(
            json.dumps(
                {
                    "segment_index": segment.segment_index,
                    "start_seconds": segment.start_seconds,
                    "end_seconds": segment.end_seconds,
                    "speaker_label": segment.speaker_label,
                    "text": segment.text,
                },
                ensure_ascii=False,
            )
            for segment in segments
        )
        return (
            f"{domain_instruction}\n\n"
            "반드시 JSON object만 반환하세요.\n"
            "segment index는 누락/중복 없이 순서대로 포함해야 합니다.\n"
            f"첫 group은 {segments[0].segment_index}에서 시작하고 마지막 group은 "
            f"{segments[-1].segment_index}에서 끝나야 합니다.\n"
            "다음 group의 segment_start_index는 반드시 이전 group의 segment_end_index + 1이어야 합니다.\n"
            "각 group은 연속된 segment range만 포함해야 합니다.\n"
            "너무 짧은 chunk를 만들지 말고, 짧은 맞장구나 보조 설명은 앞뒤 맥락에 포함하세요.\n"
            "너무 긴 chunk가 생기면 의미가 덜 깨지는 지점에서 나누세요.\n"
            "Expected JSON shape:\n"
            "{\n"
            '  "groups": [\n'
            "    {\n"
            '      "segment_start_index": 0,\n'
            '      "segment_end_index": 3,\n'
            '      "topic": "string",\n'
            '      "reason": "string",\n'
            '      "summary_hint": "string"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"Segments:\n{segment_lines}"
        )

    # LLM이 반환한 JSON을 ContextChunkPlanGroup 목록으로 변환합니다.
    # 필수 range가 없거나 타입이 맞지 않으면 상위 plan_chunks에서 fallback으로 전환됩니다.
    def _parse_plan(self, content: str) -> list[ContextChunkPlanGroup]:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Context chunk plan must be a JSON object.")
        groups = data.get("groups")
        if not isinstance(groups, list) or not groups:
            raise ValueError("Context chunk plan must include groups.")

        parsed_groups: list[ContextChunkPlanGroup] = []
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("Context chunk group must be an object.")
            parsed_groups.append(
                ContextChunkPlanGroup(
                    segment_start_index=int(group["segment_start_index"]),
                    segment_end_index=int(group["segment_end_index"]),
                    topic=self._clean_string(group.get("topic")),
                    reason=self._clean_string(group.get("reason")) or "맥락 단위 분리",
                    summary_hint=self._clean_string(group.get("summary_hint")),
                )
            )
        return parsed_groups

    # LLM plan이 실제 segment index를 누락/중복 없이 연속 range로 덮는지 검증합니다.
    # 잘못된 plan은 사용하지 않고 fallback plan으로 대체합니다.
    def _validate_plan(
        self,
        groups: list[ContextChunkPlanGroup],
        segments: list[SegmentCreate],
    ) -> None:
        expected_indices = [segment.segment_index for segment in segments]
        covered_indices: list[int] = []
        planned_ranges = [
            (group.segment_start_index, group.segment_end_index)
            for group in groups
        ]

        previous_end: int | None = None
        valid_indices = set(expected_indices)
        for group in groups:
            if group.segment_start_index > group.segment_end_index:
                raise ValueError(
                    "Context chunk group range is invalid. "
                    f"planned_ranges={planned_ranges}"
                )
            if previous_end is not None and group.segment_start_index != previous_end + 1:
                missing_indices = [
                    index
                    for index in range(previous_end + 1, group.segment_start_index)
                    if index in valid_indices
                ]
                raise ValueError(
                    "Context chunk groups must be adjacent. "
                    f"previous_end={previous_end}, "
                    f"next_start={group.segment_start_index}, "
                    f"missing_indices={missing_indices}, "
                    f"planned_ranges={planned_ranges}"
                )
            group_indices = list(range(group.segment_start_index, group.segment_end_index + 1))
            if any(index not in valid_indices for index in group_indices):
                unknown_indices = [
                    index for index in group_indices if index not in valid_indices
                ]
                raise ValueError(
                    "Context chunk group references unknown segment. "
                    f"unknown_indices={unknown_indices}, planned_ranges={planned_ranges}"
                )
            covered_indices.extend(group_indices)
            previous_end = group.segment_end_index

        if covered_indices != expected_indices:
            missing_indices = [
                index for index in expected_indices if index not in covered_indices
            ]
            duplicated_indices = [
                index for index in covered_indices if covered_indices.count(index) > 1
            ]
            raise ValueError(
                "Context chunk plan must cover all segments exactly once. "
                f"missing_indices={missing_indices}, "
                f"duplicated_indices={sorted(set(duplicated_indices))}, "
                f"planned_ranges={planned_ranges}"
            )

    # LLM plan 실패 시 transcript 처리를 유지하기 위한 시간 기준 fallback plan을 생성합니다.
    # 이 기준은 주된 chunking 전략이 아니라 과도하게 긴 chunk를 막는 안전장치입니다.
    def _fallback_plan(
        self,
        segments: list[SegmentCreate],
        error_message: str | None = None,
    ) -> list[ContextChunkPlanGroup]:
        groups: list[ContextChunkPlanGroup] = []
        current_start = segments[0]
        current_end = segments[0]

        for segment in segments[1:]:
            projected_seconds = segment.end_seconds - current_start.start_seconds
            if projected_seconds > self._lecture_fallback_max_seconds:
                groups.append(
                    self._to_fallback_group(
                        current_start,
                        current_end,
                        error_message,
                    )
                )
                current_start = segment
            current_end = segment

        groups.append(
                self._to_fallback_group(
                    current_start,
                    current_end,
                    error_message,
                )
            )
        return groups

    def _to_fallback_group(
        self,
        start_segment: SegmentCreate,
        end_segment: SegmentCreate,
        error_message: str | None,
    ) -> ContextChunkPlanGroup:
        return ContextChunkPlanGroup(
            segment_start_index=start_segment.segment_index,
            segment_end_index=end_segment.segment_index,
            topic=None,
            reason=f"LLM planner fallback: {self._lecture_fallback_max_seconds:g}초 안전 기준",
            summary_hint=None,
            planning_method="fallback",
            planning_error=error_message,
        )

    def _ordered_non_empty_segments(
        self,
        segments: list[SegmentCreate],
    ) -> list[SegmentCreate]:
        return sorted(
            [segment for segment in segments if segment.text.strip()],
            key=lambda segment: segment.segment_index,
        )

    def _clean_string(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None
