import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI

from repositories.rag_repository import RagRepository
from schemas.rag import (
    ChunkRow,
    LectureSummaryCreate,
    LectureSummaryPayload,
    LectureSummaryResponse,
)
from settings import get_settings


SYSTEM_PROMPT = (
    "You create structured lecture summaries from Korean transcript chunks. "
    "Use only facts present in the chunks. Write in Korean and return only valid JSON."
)


class LectureSummaryService:
    def __init__(self, repository: RagRepository) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OPENAI_API_KEY is not configured.",
            )
        self._repository = repository
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_summary_model

    # transcript와 저장된 chunks를 기반으로 강의 요약 데이터 payload를 생성하거나 기존 payload를 반환한다.
    # 기능 흐름:
    #   1. transcript 소유권/상태/full_text를 검증한다
    #   2. 기존 lecture_summaries row가 있으면 LLM 재호출 없이 반환한다
    #   3. chunks를 조회해 LLM 입력으로 압축한다
    #   4. overview/contexts/keywords JSON을 생성하고 정규화한 뒤 저장한다
    # 파라미터:
    #   transcript_id: 요약할 transcript UUID
    #   user_id: 인증된 사용자 UUID
    async def get_or_create_summary(
        self,
        transcript_id: UUID,
        user_id: UUID,
    ) -> LectureSummaryResponse:
        # 1. 인증 사용자 소유 transcript만 조회
        started_at = datetime.now(timezone.utc)
        transcript = await self._repository.get_transcript_by_id(transcript_id, user_id)
        if transcript is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transcript not found.",
            )
        await self._raise_if_cancel_requested(transcript_id, user_id, started_at)
        if transcript.status != "completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Transcript is not completed.",
            )
        if not transcript.full_text or not transcript.full_text.strip():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Transcript has no text to summarize.",
            )

        # 2. 이미 생성된 요약 payload가 있으면 그대로 반환
        existing = await self._repository.get_lecture_summary_by_transcript(
            transcript_id,
            user_id,
        )
        if existing is not None:
            return self._to_response(
                summary_id=existing.id,
                transcript_id=existing.transcript_id,
                payload=self._normalize_payload(
                    existing.payload,
                    [],
                    transcript.title,
                ),
            )

        # 3. 맥락 단위 chunk 기반 기능이므로 chunk pipeline 완료 후에만 생성한다
        chunks = await self._repository.fetch_chunks_by_transcript(transcript_id)
        if not chunks:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Transcript chunks are not ready.",
            )

        # 4. LLM 생성 → payload 정규화 → 저장
        await self._raise_if_cancel_requested(transcript_id, user_id, started_at)
        raw_payload = await self._create_payload(
            title=transcript.title,
            chunks=chunks,
        )
        await self._raise_if_cancel_requested(transcript_id, user_id, started_at)
        payload = self._normalize_payload(raw_payload, chunks, transcript.title)
        summary_id = await self._repository.insert_lecture_summary(
            LectureSummaryCreate(
                transcript_id=transcript_id,
                user_id=user_id,
                payload=payload,
                model=self._model,
            )
        )
        return self._to_response(
            summary_id=summary_id,
            transcript_id=transcript_id,
            payload=payload,
        )

    # OpenAI chat completion을 호출해 overview/contexts/keywords 구조의 JSON payload를 요청한다.
    # chunks의 topic/summary/metadata를 함께 전달해 전체 원문보다 안정적인 맥락 단위 요약을 만든다.
    async def _raise_if_cancel_requested(
        self,
        transcript_id: UUID,
        user_id: UUID,
        started_at: datetime,
    ) -> None:
        checker = getattr(
            self._repository,
            "is_processing_cancel_requested_after",
            None,
        )
        if checker is not None and await checker(transcript_id, user_id, started_at):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Processing was cancelled by user.",
            )

    async def _create_payload(
        self,
        title: str | None,
        chunks: list[ChunkRow],
    ) -> dict[str, Any]:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self._build_prompt(title, chunks)},
                ],
            )
        except APIError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Summary provider failed.",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Summary generation failed.",
            ) from exc

        content = response.choices[0].message.content
        if not content or not content.strip():
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Summary provider returned an empty response.",
            )

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Summary provider returned invalid JSON.",
            ) from exc

        return data if isinstance(data, dict) else {}

    # LLM에게 전달할 강의 chunk 재료와 기대 JSON 스키마를 구성한다.
    # 파라미터:
    #   title: transcript 제목. 없으면 프롬프트에서 생략
    #   chunks: chunk_index 순으로 조회된 강의 맥락 chunk 목록
    def _build_prompt(self, title: str | None, chunks: list[ChunkRow]) -> str:
        chunk_payload = [
            {
                "index": chunk.chunk_index,
                "topic": chunk.topic,
                "summary": chunk.summary,
                "keywords": chunk.keywords,
                "concepts": self._clean_strings(chunk.metadata.get("concepts")),
                "learning_points": self._clean_strings(
                    chunk.metadata.get("learning_points")
                ),
                "summary_hint": self._clean_string(chunk.metadata.get("summary_hint")),
                "start_seconds": chunk.start_seconds,
                "end_seconds": chunk.end_seconds,
                "segment_start_index": chunk.segment_start_index,
                "segment_end_index": chunk.segment_end_index,
                "text": chunk.text,
            }
            for chunk in chunks
        ]
        title_line = f"강의 제목: {title}\n" if title else ""
        return (
            f"{title_line}"
            "다음 강의 transcript chunks를 바탕으로 전체 강의 요약 데이터를 만드세요.\n"
            "녹취에 없는 내용은 만들지 말고, 각 contexts 항목은 chunk의 시간/segment 범위를 보존하세요.\n"
            "반드시 JSON object만 반환하세요.\n"
            "Expected JSON shape:\n"
            "{\n"
            '  "overview": {\n'
            '    "title": "강의 제목",\n'
            '    "summary": "전체 맥락 요약",\n'
            '    "key_points": ["핵심 요점"]\n'
            "  },\n"
            '  "contexts": [\n'
            "    {\n"
            '      "index": 0,\n'
            '      "topic": "핵심 주제",\n'
            '      "subtitle": "소제목",\n'
            '      "content": "해당 맥락 단위의 설명",\n'
            '      "keywords": ["핵심어"],\n'
            '      "concepts": ["개념"],\n'
            '      "learning_points": ["학습 포인트"],\n'
            '      "start_seconds": 0,\n'
            '      "end_seconds": 120,\n'
            '      "segment_start_index": 0,\n'
            '      "segment_end_index": 8\n'
            "    }\n"
            "  ],\n"
            '  "keywords": [\n'
            "    {\n"
            '      "keyword": "핵심어",\n'
            '      "summary": "강의에서 이 키워드가 다뤄진 내용",\n'
            '      "related_context_indices": [0]\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"Chunks:\n{json.dumps(chunk_payload, ensure_ascii=False)}"
        )

    # LLM 또는 기존 DB payload를 API 응답 스키마에 맞게 보정한다.
    # 누락된 overview/contexts/keywords는 빈 값이나 chunk 기반 fallback으로 채운다.
    def _normalize_payload(
        self,
        payload: dict[str, Any],
        chunks: list[ChunkRow],
        title: str | None,
    ) -> dict[str, Any]:
        overview = payload.get("overview")
        overview = overview if isinstance(overview, dict) else {}
        contexts = self._normalize_contexts(payload.get("contexts"), chunks)
        keywords = self._normalize_keywords(payload.get("keywords"), chunks)

        return {
            "overview": {
                "title": self._clean_string(overview.get("title")) or title or "",
                "summary": self._clean_string(overview.get("summary")) or "",
                "key_points": self._clean_strings(overview.get("key_points")),
            },
            "contexts": contexts,
            "keywords": keywords,
        }

    def _normalize_contexts(
        self,
        value: Any,
        chunks: list[ChunkRow],
    ) -> list[dict[str, Any]]:
        raw_items = value if isinstance(value, list) else []
        contexts: list[dict[str, Any]] = []
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            contexts.append(
                {
                    "index": self._to_int(item.get("index"), index),
                    "topic": self._clean_string(item.get("topic")) or "",
                    "subtitle": self._clean_string(item.get("subtitle")) or "",
                    "content": self._clean_string(item.get("content")) or "",
                    "keywords": self._clean_strings(item.get("keywords")),
                    "concepts": self._clean_strings(item.get("concepts")),
                    "learning_points": self._clean_strings(
                        item.get("learning_points")
                    ),
                    "start_seconds": self._optional_float(item.get("start_seconds")),
                    "end_seconds": self._optional_float(item.get("end_seconds")),
                    "segment_start_index": self._optional_int(
                        item.get("segment_start_index")
                    ),
                    "segment_end_index": self._optional_int(
                        item.get("segment_end_index")
                    ),
                }
            )

        if contexts:
            return contexts

        return [
            {
                "index": index,
                "topic": chunk.topic or "",
                "subtitle": chunk.topic or "",
                "content": chunk.summary or chunk.text,
                "keywords": chunk.keywords,
                "concepts": self._clean_strings(chunk.metadata.get("concepts")),
                "learning_points": self._clean_strings(
                    chunk.metadata.get("learning_points")
                ),
                "start_seconds": chunk.start_seconds,
                "end_seconds": chunk.end_seconds,
                "segment_start_index": chunk.segment_start_index,
                "segment_end_index": chunk.segment_end_index,
            }
            for index, chunk in enumerate(chunks)
        ]

    def _normalize_keywords(
        self,
        value: Any,
        chunks: list[ChunkRow],
    ) -> list[dict[str, Any]]:
        raw_items = value if isinstance(value, list) else []
        keywords: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            keyword = self._clean_string(item.get("keyword"))
            if not keyword:
                continue
            keywords.append(
                {
                    "keyword": keyword,
                    "summary": self._clean_string(item.get("summary")) or "",
                    "related_context_indices": self._clean_ints(
                        item.get("related_context_indices")
                    ),
                }
            )

        if keywords:
            return keywords

        related_by_keyword: dict[str, list[int]] = {}
        for index, chunk in enumerate(chunks):
            for keyword in chunk.keywords:
                related_by_keyword.setdefault(keyword, []).append(index)

        return [
            {
                "keyword": keyword,
                "summary": "",
                "related_context_indices": indices,
            }
            for keyword, indices in related_by_keyword.items()
        ]

    # DB 저장용 payload dict를 명시 응답 모델로 펼쳐서 변환한다.
    # 필요성: 저장소는 JSONB payload를 유지하되 API는 overview/contexts/keywords를 최상위로 보여준다.
    def _to_response(
        self,
        summary_id: UUID,
        transcript_id: UUID,
        payload: dict[str, Any],
        persona_id: str = "general",
    ) -> LectureSummaryResponse:
        """
        기능 요약: 정규화된 payload dict를 LectureSummaryResponse로 변환한다.

        기능 흐름:
            1. LectureSummaryPayload로 payload 구조를 한 번 더 검증
            2. model_dump() 결과를 응답 모델 최상위 필드로 펼쳐 반환

        파라미터:
            summary_id: lecture_summaries row id
            transcript_id: 요약 대상 transcript id
            payload: {"overview": ..., "contexts": ..., "keywords": ...} 형태의 dict
            persona_id: 현재 요약 Persona. 아직 요청 스키마가 없으므로 기본값 general 사용
        """
        # 1. payload 구조 검증 및 타입 모델 변환
        typed_payload = LectureSummaryPayload.model_validate(payload)

        # 2. payload 래퍼 없이 API 응답 최상위로 펼쳐 반환
        return LectureSummaryResponse(
            summary_id=summary_id,
            transcript_id=transcript_id,
            persona_id=persona_id,
            overview=typed_payload.overview,
            contexts=typed_payload.contexts,
            keywords=typed_payload.keywords,
        )

    def _clean_string(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    def _clean_strings(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        cleaned: list[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    def _clean_ints(self, values: Any) -> list[int]:
        if not isinstance(values, list):
            return []
        cleaned: list[int] = []
        for value in values:
            parsed = self._optional_int(value)
            if parsed is not None and parsed not in cleaned:
                cleaned.append(parsed)
        return cleaned

    def _to_int(self, value: Any, default: int) -> int:
        parsed = self._optional_int(value)
        return parsed if parsed is not None else default

    def _optional_int(self, value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _optional_float(self, value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
