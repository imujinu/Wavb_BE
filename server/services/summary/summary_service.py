import asyncio
import json

from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI

from settings import get_settings


SYSTEM_PROMPT = (
    "You are a document summarization assistant for Korean voice transcripts. "
    "Do not invent content that is not present in the transcript. "
    "Summarize clearly around key points, decisions, and follow-up tasks."
)


class SummaryService:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OPENAI_API_KEY is not configured.",
            )
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_summary_model
        self._text_chunk_chars = settings.summary_text_chunk_chars
        self._summary_concurrency = settings.summary_concurrency

    async def summarize(self, transcript: str) -> str:
        if not transcript.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Transcript cannot be empty.",
            )
        chunks = self._split_text(transcript.strip(), self._text_chunk_chars)
        # 1. 반환된 청크가 하나라면 그대로 요약 생성, 
        if len(chunks) == 1:
            return await self._create_summary(chunks[0])

        # 2. 반환된 청크가 여러개라면, 각 청크를 비동기적으로 다시 요약
        partial_summaries = await self._summarize_chunks(chunks)

        # 2-1. 각 청크별로 반환된 요약들을 하나의 텍스트로 합침. 
        # 이 때, 단순히 요약들을 나열하는 것이 아니라, 
        # 각 요약이 어떤 청크에서 나온 것인지 구분할 수 있도록 
        # "Partial summary {index}:" 형태로 구분자를 추가하여 나열
        final_input = "\n\n".join(
            f"Partial summary {index + 1}:\n{summary}"
            for index, summary in enumerate(partial_summaries)
        )
        return await self._create_summary(
            final_input,
            instruction=(
                "Combine the following partial summaries into one Korean summary. "
                "Remove repetition, preserve important decisions and follow-up tasks, "
                "and keep the result concise."
            ),
        )

    async def summarize_with_keywords(self, transcript: str) -> tuple[str, list[str]]:
        # 1. 빈 텍스트 가드 — 빈 입력으로 LLM을 호출하지 않는다.
        if not transcript.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Transcript cannot be empty.",
            )

        # 2. 요약 + 키워드를 JSON 한 덩어리로 요청
        prompt = (
            "다음 한국어 음성 전사 구간을 분석해 JSON만 출력하라.\n"
            '형식: {"summary": "2~3문장 한국어 요약", "keywords": ["핵심어", ...]}\n'
            "- keywords는 2~3개, 전사에 실제 등장한 핵심 명사/개념만 담을 것.\n"
            "- 전사에 없는 내용을 지어내지 말 것."
        )
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0.3,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{prompt}\n\nText:\n{transcript.strip()}"},
                ],
            )
            data = json.loads(response.choices[0].message.content or "{}")
        except Exception:
            # 4. 폴백: 키워드 추출 실패 시 요약만이라도 보장 (keywords는 빈 배열)
            return await self.summarize(transcript), []

        # 3. 파싱 결과 정리 — summary 문자열, keywords는 비어있지 않은 문자열 최대 6개
        summary = (data.get("summary") or "").strip()
        keywords = [
            kw.strip()
            for kw in (data.get("keywords") or [])
            if isinstance(kw, str) and kw.strip()
        ][:6]
        # summary가 비면(모델이 형식을 벗어난 경우) 기존 경로로 재생성
        if not summary:
            return await self.summarize(transcript), keywords
        return summary, keywords

    async def _summarize_chunks(self, chunks: list[str]) -> list[str]:
        if self._summary_concurrency <= 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUMMARY_CONCURRENCY must be greater than zero.",
            )

        semaphore = asyncio.Semaphore(self._summary_concurrency)
        tasks = [
            asyncio.create_task(self._summarize_chunk(index, chunk, semaphore))
            for index, chunk in enumerate(chunks)
        ]

        try:
            indexed_summaries = await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            raise

        return [
            summary
            for _, summary in sorted(indexed_summaries, key=lambda item: item[0])
        ]

    async def _summarize_chunk(
        self,
        index: int,
        chunk: str,
        semaphore: asyncio.Semaphore,
    ) -> tuple[int, str]:
        async with semaphore:
            summary = await self._create_summary(
                chunk,
                instruction=(
                    "Summarize this section of a longer Korean voice transcript. "
                    "Focus on key points, decisions, and follow-up tasks."
                ),
            )
        return index, summary

    async def _create_summary(
        self,
        text: str,
        instruction: str | None = None,
    ) -> str:
        prompt = instruction or (
            "Summarize the following Korean voice transcript in Korean.\n"
            "- Include 3 to 5 key points.\n"
            "- Include a 2 to 3 sentence overall summary.\n"
            "- Include important follow-up tasks if present."
        )

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{prompt}\n\nText:\n{text}"},
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

        summary = response.choices[0].message.content
        if not summary or not summary.strip():
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Summary provider returned an empty response.",
            )

        return summary.strip()

    def _split_text(self, text: str, max_chars: int) -> list[str]:
        if max_chars <= 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUMMARY_TEXT_CHUNK_CHARS must be greater than zero.",
            )

        remaining = text.strip()
        if len(remaining) <= max_chars:
            return [remaining]

        chunks: list[str] = []
        while len(remaining) > max_chars:
            split_at = remaining.rfind("\n", 0, max_chars + 1)
            if split_at < max_chars // 2:
                split_at = remaining.rfind(" ", 0, max_chars + 1)
            if split_at < max_chars // 2:
                split_at = max_chars

            chunk = remaining[:split_at].strip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[split_at:].strip()

        if remaining:
            chunks.append(remaining)

        return chunks
