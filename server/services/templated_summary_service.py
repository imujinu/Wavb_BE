# 선택한 스크립트(transcript)를 선택한 템플릿 섹션 스키마에 맞는 구조화 JSON으로 요약하는 서비스.
#
# 기능 요약:
#   - LLM에게는 "섹션 key 별 JSON" 만 생성하도록 강제(response_format=json_object)하고,
#     PDF 레이아웃은 후단 SummaryPdfService가 담당하도록 책임을 분리한다.
#   - 이렇게 하면 출력이 결정적이고, 잘못된 섹션만 골라 수정→재렌더하기 쉽다.

import json

from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI

from services.pdf_templates import TemplateSpec
from settings import get_settings


SYSTEM_PROMPT = (
    "You convert Korean voice transcripts into structured summary documents. "
    "Use only the content present in the transcript and never invent facts. "
    "Write in Korean and return only valid JSON."
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

    # 스크립트 텍스트를 템플릿 섹션 스키마에 맞춘 구조화 dict로 요약한다.
    # 동작 흐름:
    #   1. 빈 텍스트 방어 → 422
    #   2. 입력이 상한을 넘으면 앞부분만 사용(1차 단순 처리)
    #   3. _create_structured_summary()로 섹션별 JSON 생성
    #   4. _normalize_payload()로 누락 섹션을 빈 값으로 보정
    # 파라미터:
    #   transcript_text: 요약 대상 원문 (예: "오늘 회의에서는 ...")
    #   template: 생성 기준이 되는 폼 명세(TemplateSpec)
    #   title: transcript 제목 (프롬프트 맥락 보강용, 없으면 None)
    #   domain_type: "meeting" | "lecture" 등 도메인 힌트
    async def summarize_for_template(
        self,
        transcript_text: str,
        template: TemplateSpec,
        title: str | None = None,
        domain_type: str | None = None,
    ) -> dict:
        # 1. 빈 텍스트 방어
        if not transcript_text or not transcript_text.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Transcript cannot be empty.",
            )

        # 2. 입력 상한 초과 시 앞부분만 사용 (토큰 초과/지연 폭증 방지)
        text = transcript_text.strip()
        if len(text) > self._max_input_chars:
            text = text[: self._max_input_chars]

        # 3. LLM으로 섹션별 구조화 JSON 생성
        raw_payload = await self._create_structured_summary(
            text=text,
            template=template,
            title=title,
            domain_type=domain_type,
        )

        # 4. 누락/타입 오류 섹션을 빈 값으로 보정해 PDF 렌더가 항상 안정적으로 동작하도록 한다
        return self._normalize_payload(raw_payload, template)

    # OpenAI chat completion을 호출해 섹션 스키마에 맞는 JSON을 생성한다.
    # _build_prompt()로 섹션 지시문을 포함한 프롬프트를 만들어 전달한다.
    async def _create_structured_summary(
        self,
        text: str,
        template: TemplateSpec,
        title: str | None,
        domain_type: str | None,
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
                        "content": self._build_prompt(text, template, title, domain_type),
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
        domain_type: str | None,
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
        if domain_type:
            header_context.append(f"유형: {domain_type}")
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
