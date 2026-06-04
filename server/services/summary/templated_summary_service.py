# 선택한 스크립트(transcript)를 선택한 템플릿 섹션 스키마에 맞는 구조화 JSON으로 요약하는 서비스.
#
# 기능 요약:
#   - LLM에게는 "섹션 key 별 JSON" 만 생성하도록 강제(response_format=json_object)하고,
#     PDF 레이아웃은 후단 SummaryPdfService가 담당하도록 책임을 분리한다.
#   - 짧은 입력은 단일 LLM 호출, 긴 입력은 map-reduce(윈도우별 부분요약 → 통합 구조화)로 처리해
#     내용 손실 없이 임의 길이의 녹취록을 안정적으로 요약한다.

import asyncio
import json

from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI

from services.summary.pdf_templates import TemplateSpec
from settings import get_settings


SYSTEM_PROMPT = (
    "You convert Korean voice transcripts into structured summary documents. "
    "Use only the content present in the transcript and never invent facts. "
    "Write in Korean and return only valid JSON."
)

MAP_SYSTEM_PROMPT = (
    "You are a Korean voice transcript analysis assistant. "
    "Extract and preserve all relevant facts from the given transcript section. "
    "Do not invent content that is not present. Write in Korean."
)


class TemplatedSummaryService:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OPENAI_API_KEY is not configured.",
            )
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.summary_pdf_model
        self._max_input_chars = settings.summary_pdf_max_input_chars
        self._text_chunk_chars = settings.summary_text_chunk_chars
        self._summary_concurrency = settings.summary_concurrency

    # 스크립트 텍스트를 템플릿 섹션 스키마에 맞춘 구조화 dict로 요약한다.
    # 동작 흐름:
    #   1. 빈 텍스트 방어 → 422
    #   2. len(text) <= _max_input_chars → 단일 LLM 호출(짧은 경로)
    #      else → map-reduce(긴 경로): 윈도우 분할 → 병렬 부분요약 → 통합 구조화
    #   3. _normalize_payload()로 누락 섹션을 빈 값으로 보정
    # 파라미터:
    #   transcript_text: 요약 대상 원문 (예: "오늘 회의에서는 ...")
    #   template: 생성 기준이 되는 폼 명세(TemplateSpec)
    #   title: transcript 제목 (프롬프트 맥락 보강용, 없으면 None)
    async def summarize_for_template(
        self,
        transcript_text: str,
        template: TemplateSpec,
        title: str | None = None,
    ) -> dict:
        # 1. 빈 텍스트 방어
        if not transcript_text or not transcript_text.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Transcript cannot be empty.",
            )

        text = transcript_text.strip()

        if len(text) <= self._max_input_chars:
            # 2. 짧은 입력: 단일 LLM 호출
            raw_payload = await self._create_structured_summary(
                text=text,
                template=template,
                title=title,
            )
        else:
            # 3. 긴 입력: map-reduce
            raw_payload = await self._map_reduce_summary(
                text=text,
                template=template,
                title=title,
            )

        # 4. 누락/타입 오류 섹션을 빈 값으로 보정해 PDF 렌더가 항상 안정적으로 동작하도록 한다
        return self._normalize_payload(raw_payload, template)

    # 긴 텍스트를 윈도우로 분할해 병렬 부분요약(map) 후 통합 구조화(reduce)한다.
    # 동작 흐름:
    #   1. _split_text()로 윈도우 목록 생성
    #   2. _summarize_windows()로 각 윈도우를 병렬 부분요약 → 리스트
    #   3. 부분요약 합산이 _max_input_chars 초과 시 1회 재분할 요약(재귀 방지)
    #   4. _create_structured_summary()로 부분요약 묶음을 최종 JSON으로 reduce
    # 파라미터:
    #   text: _max_input_chars 초과 원문
    #   template: 섹션 명세
    async def _map_reduce_summary(
        self,
        text: str,
        template: TemplateSpec,
        title: str | None,
    ) -> dict:
        # 1. 윈도우 분할
        windows = self._split_text(text, self._text_chunk_chars)

        # 2. 병렬 부분요약(map)
        partial_summaries = await self._summarize_windows(windows, template)
        combined = "\n\n".join(
            f"[부분요약 {i + 1}]\n{s}" for i, s in enumerate(partial_summaries)
        )

        # 3. 부분요약 합산이 여전히 상한 초과 시 한 번 더 분할 요약(재귀 1회)
        if len(combined) > self._max_input_chars:
            sub_windows = self._split_text(combined, self._text_chunk_chars)
            sub_summaries = await self._summarize_windows(sub_windows, template)
            combined = "\n\n".join(
                f"[부분요약 {i + 1}]\n{s}" for i, s in enumerate(sub_summaries)
            )

        # 4. reduce: 부분요약 묶음을 섹션 JSON으로 구조화
        return await self._create_structured_summary(
            text=combined,
            template=template,
            title=title,
        )

    # 윈도우 목록을 Semaphore로 동시 실행 수를 제한하며 병렬 부분요약한다.
    # 동작 흐름:
    #   1. asyncio.Semaphore(_summary_concurrency)로 동시 호출 수 제한
    #   2. 각 윈도우에 대해 _summarize_window() 태스크 생성
    #   3. gather로 병렬 실행 — 하나라도 실패하면 잔여 태스크 취소 후 예외 전파
    #   4. 인덱스 기준 정렬해 원문 순서 보존
    # 파라미터:
    #   windows: _split_text()가 반환한 텍스트 구간 목록
    #   template: 섹션 label을 map 프롬프트에 전달하기 위한 명세
    async def _summarize_windows(
        self,
        windows: list[str],
        template: TemplateSpec,
    ) -> list[str]:
        semaphore = asyncio.Semaphore(self._summary_concurrency)
        tasks = [
            asyncio.create_task(self._summarize_window(idx, window, template, semaphore))
            for idx, window in enumerate(windows)
        ]
        try:
            # 1. 병렬 실행
            indexed = await asyncio.gather(*tasks)
        except Exception:
            # 2. 실패 시 잔여 태스크 취소
            for task in tasks:
                if not task.done():
                    task.cancel()
            raise

        # 3. 인덱스 정렬로 원문 순서 보존
        return [s for _, s in sorted(indexed, key=lambda x: x[0])]

    # 단일 윈도우를 템플릿 섹션 관점으로 부분 요약한다(map 단계).
    # 섹션 label을 프롬프트에 포함해 해당 구간에서 각 섹션 관련 사실을 빠짐없이 보존하도록 유도한다.
    # 출력은 자유 텍스트(불릿 허용) — JSON 구조화는 reduce(_create_structured_summary)에서만 수행.
    # 동작 흐름:
    #   1. Semaphore 획득 후 LLM 호출
    #   2. 응답 검증 → (인덱스, 부분요약) 반환
    # 파라미터:
    #   index: 원문 순서 복원용 인덱스
    #   window: 처리할 텍스트 구간
    #   template: 섹션 label 목록 추출용 명세
    #   semaphore: 동시 실행 제한용 세마포어
    async def _summarize_window(
        self,
        index: int,
        window: str,
        template: TemplateSpec,
        semaphore: asyncio.Semaphore,
    ) -> tuple[int, str]:
        async with semaphore:
            # 1. 템플릿 섹션 label을 프롬프트에 포함해 관련 사실 보존 지시
            section_labels = ", ".join(f'"{s.label}"' for s in template.sections)
            prompt = (
                f"다음 녹취록 구간을 읽고, {section_labels} 섹션에 해당하는 "
                "사실을 빠짐없이 보존하는 부분 요약을 작성하세요. "
                "불릿 또는 산문 형식으로 자유롭게 작성하고, JSON은 사용하지 마세요. "
                "내용을 임의로 만들지 마세요.\n\n"
                f"녹취록 구간:\n{window}"
            )
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    temperature=0.2,
                    messages=[
                        {"role": "system", "content": MAP_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
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

            # 2. (인덱스, 부분요약) 반환으로 gather 후 순서 복원 가능하게 함
            return index, content.strip()

    # OpenAI chat completion을 호출해 섹션 스키마에 맞는 JSON을 생성한다.
    # _build_prompt()로 섹션 지시문을 포함한 프롬프트를 만들어 전달한다.
    async def _create_structured_summary(
        self,
        text: str,
        template: TemplateSpec,
        title: str | None,
    ) -> dict:
        try:
            # 1. 섹션 지시문 + 출력 스키마를 담은 프롬프트로 요약 요청
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": self._build_prompt(text, template, title),
                    },
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

        # 2. JSON 파싱 — 모델이 형식을 어기면 502로 변환
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Summary provider returned invalid JSON.",
            ) from exc

        return data if isinstance(data, dict) else {}

    # 템플릿 섹션별 작성 지시문과 기대 출력 스키마를 담은 프롬프트를 만든다.
    # 각 섹션의 key/label/description을 그대로 전달해 LLM이 섹션 의도를 정확히 파악하도록 한다.
    def _build_prompt(
        self,
        text: str,
        template: TemplateSpec,
        title: str | None,
    ) -> str:
        # 1. 섹션별 지시문 나열 (key — label: description)
        section_lines = "\n".join(
            f'- "{section.key}" ({section.label}): {section.description}'
            for section in template.sections
        )
        # 2. 기대 출력 JSON 스키마 (모든 섹션 key 포함, 값은 문자열 또는 문자열 배열 허용)
        schema_keys = ",\n".join(f'  "{section.key}": ""' for section in template.sections)

        header_context = []
        if title:
            header_context.append(f"제목: {title}")
        context_line = ("\n".join(header_context) + "\n") if header_context else ""

        return (
            f"다음 녹취록을 '{template.name}' 양식으로 요약하세요.\n"
            "녹취록에 실제로 존재하는 내용만 사용하고, 근거가 없는 섹션은 빈 문자열 또는 빈 배열로 두세요.\n"
            "각 값은 문자열 또는 문자열 배열로 작성하세요(목록 성격이면 배열 권장).\n\n"
            f"섹션 지시:\n{section_lines}\n\n"
            "반드시 아래 key를 가진 JSON object만 반환하세요:\n"
            "{\n"
            f"{schema_keys}\n"
            "}\n\n"
            f"{context_line}"
            f"녹취록:\n{text}"
        )

    # 텍스트를 max_chars 기준으로 줄/공백 경계를 우선해 분할한다.
    # 문장 중간 절단을 최소화하기 위해 개행 → 공백 → 강제 절단 순으로 분할 지점을 탐색한다.
    # 동작 흐름:
    #   1. 전체 길이가 max_chars 이하면 그대로 반환
    #   2. 개행 → 공백 → 강제 절단 순으로 분할 지점 결정
    #   3. 남은 텍스트가 없을 때까지 반복
    # 파라미터:
    #   text: 분할 대상 텍스트
    #   max_chars: 청크 최대 글자 수 (예: 16000)
    def _split_text(self, text: str, max_chars: int) -> list[str]:
        remaining = text.strip()
        if len(remaining) <= max_chars:
            return [remaining]

        chunks: list[str] = []
        while len(remaining) > max_chars:
            # 1. 개행 경계 우선 탐색
            split_at = remaining.rfind("\n", 0, max_chars + 1)
            # 2. 개행이 너무 앞쪽이면 공백 경계 탐색
            if split_at < max_chars // 2:
                split_at = remaining.rfind(" ", 0, max_chars + 1)
            # 3. 적절한 경계가 없으면 강제 절단
            if split_at < max_chars // 2:
                split_at = max_chars

            chunk = remaining[:split_at].strip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[split_at:].strip()

        if remaining:
            chunks.append(remaining)

        return chunks

    # LLM 응답을 템플릿 섹션 기준으로 보정한다.
    # 누락된 섹션 key는 빈 문자열로 채우고, 허용 타입(str / list[str]) 외 값은 안전하게 정규화한다.
    def _normalize_payload(self, raw_payload: dict, template: TemplateSpec) -> dict:
        normalized: dict = {}
        for section in template.sections:
            value = raw_payload.get(section.key)
            normalized[section.key] = self._normalize_value(value)
        return normalized

    # 단일 섹션 값을 문자열 또는 문자열 리스트로 정규화한다.
    def _normalize_value(self, value) -> str | list[str]:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if value is None:
            return ""
        # 숫자 등 그 외 타입은 문자열로 변환해 렌더 단계에서 안전하게 처리
        return str(value)
