import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI

from schemas.rag import ChunkCreate
from services.files.processing_cancellation import raise_if_cancel_requested
from settings import get_settings


SYSTEM_PROMPT = (
    "You enrich Korean voice transcript chunks for summary material generation. "
    "Do not invent facts that are not present in the chunk. "
    "Return only valid JSON."
)


@dataclass(frozen=True)
class ChunkMetadata:
    topic: str | None
    subtopic: str | None
    keywords: list[str]
    summary: str | None
    extra_metadata: dict[str, Any]


class ChunkMetadataService:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OPENAI_API_KEY is not configured.",
            )
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_summary_model
        self._metadata_concurrency = settings.summary_concurrency

    # 청크 목록을 받아 요약자료 생성에 사용할 topic, keyword, summary metadata를 생성합니다.
    # 각 청크는 독립적으로 처리되며, 한 청크의 실패가 전체 저장 흐름을 막지 않도록 원본 청크로 대체합니다.
    async def enrich_chunks(
        self,
        chunks: list[ChunkCreate],
        cancellation_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> list[ChunkCreate]:
        if not chunks:
            return []
        await raise_if_cancel_requested(cancellation_checker)
        if self._metadata_concurrency <= 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUMMARY_CONCURRENCY must be greater than zero.",
            )

        semaphore = asyncio.Semaphore(self._metadata_concurrency)
        tasks = [
            asyncio.create_task(
                self._enrich_chunk(index, chunk, semaphore, cancellation_checker)
            )
            for index, chunk in enumerate(chunks)
        ]

        try:
            enriched_chunks = await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            raise

        await raise_if_cancel_requested(cancellation_checker)
        return [
            chunk
            for _, chunk in sorted(enriched_chunks, key=lambda item: item[0])
        ]

    # 단일 청크 metadata를 생성하고 ChunkCreate에 반영합니다.
    # provider 응답 오류나 JSON 파싱 오류가 발생하면 원본 청크를 그대로 반환합니다.
    async def _enrich_chunk(
        self,
        index: int,
        chunk: ChunkCreate,
        semaphore: asyncio.Semaphore,
        cancellation_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[int, ChunkCreate]:
        try:
            async with semaphore:
                metadata = await self._create_metadata(chunk)
        except Exception:
            return index, chunk

        return index, self._apply_metadata(chunk, metadata)

    # OpenAI chat completion을 호출해 강의 청크별 metadata JSON을 생성합니다.
    # concepts와 learning_points를 중심으로 후속 강의 요약 데이터의 재료를 만든다.
    async def _create_metadata(self, chunk: ChunkCreate) -> ChunkMetadata:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self._build_prompt(chunk)},
                ],
            )
        except APIError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Chunk metadata provider failed.",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Chunk metadata generation failed.",
            ) from exc

        content = response.choices[0].message.content
        if not content or not content.strip():
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Chunk metadata provider returned an empty response.",
            )

        return self._parse_metadata(content)

    # 강의 요약 데이터 생성에 필요한 metadata 필드를 요구하는 프롬프트를 생성합니다.
    # concepts는 핵심 개념, learning_points는 학습자가 기억할 요점을 담는다.
    def _build_prompt(self, chunk: ChunkCreate) -> str:
        common_instruction = (
            "다음 transcript chunk만 근거로 요약자료 생성용 metadata를 생성하세요.\n"
            "한국어로 작성하고, 근거가 없으면 빈 문자열 또는 빈 배열을 사용하세요.\n"
            "반드시 JSON object만 반환하세요.\n"
            "공통 필드: topic, subtopic, keywords, summary\n"
        )
        domain_instruction = (
            "강의 chunk입니다. metadata 필드에는 concepts, learning_points 배열을 포함하세요.\n"
            "concepts는 핵심 개념명, learning_points는 학습자가 기억해야 할 요점을 넣으세요."
        )

        return (
            f"{common_instruction}\n"
            f"{domain_instruction}\n\n"
            "Expected JSON shape:\n"
            "{\n"
            '  "topic": "string",\n'
            '  "subtopic": "string",\n'
            '  "keywords": ["string"],\n'
            '  "summary": "string",\n'
            '  "metadata": { "domain_specific_key": ["string"] }\n'
            "}\n\n"
            f"Chunk text:\n{chunk.text}"
        )

    # provider가 반환한 JSON 문자열을 안전하게 파싱하고 강의 metadata만 남깁니다.
    # 잘못된 타입이나 빈 문자열은 제거해 DB에 저장되는 metadata 품질을 일정하게 맞춥니다.
    def _parse_metadata(self, raw_content: str) -> ChunkMetadata:
        data = json.loads(raw_content)
        if not isinstance(data, dict):
            raise ValueError("Chunk metadata response must be a JSON object.")

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        extra_metadata = self._extract_extra_metadata(metadata)
        return ChunkMetadata(
            topic=self._clean_string(data.get("topic")),
            subtopic=self._clean_string(data.get("subtopic")),
            keywords=self._clean_strings(data.get("keywords")),
            summary=self._clean_string(data.get("summary")),
            extra_metadata=extra_metadata,
        )

    # 강의 요약 생성에 필요한 metadata key만 추려서 저장합니다.
    # concepts와 learning_points 외의 LLM 부가 필드는 저장하지 않는다.
    def _extract_extra_metadata(
        self,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "concepts": self._clean_strings(metadata.get("concepts")),
            "learning_points": self._clean_strings(metadata.get("learning_points")),
        }

    # 생성된 metadata를 기존 ChunkCreate 값과 병합합니다.
    # chunk_builder가 넣은 segment_count, chunk_goal 같은 기존 metadata는 덮어쓰지 않고 유지합니다.
    def _apply_metadata(
        self,
        chunk: ChunkCreate,
        metadata: ChunkMetadata,
    ) -> ChunkCreate:
        merged_metadata = {**chunk.metadata, **metadata.extra_metadata}
        return ChunkCreate(
            **{
                **chunk.model_dump(),
                "topic": metadata.topic,
                "subtopic": metadata.subtopic,
                "keywords": metadata.keywords,
                "summary": metadata.summary,
                "metadata": merged_metadata,
            }
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
            if not isinstance(value, str):
                continue
            value = value.strip()
            if value and value not in cleaned:
                cleaned.append(value)
        return cleaned
