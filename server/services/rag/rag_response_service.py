# RAG 파이프라인의 생성(Generation) 단계를 담당하는 서비스.
# RagQueryService가 검색한 parent chunk 목록을 context로 받아,
# 사용자 질의에 대한 한국어 자연어 답변을 LLM으로 생성한다.
# 검색 결과를 그대로 반환하는 단순 검색과 달리, 제공된 청크 범위 내에서만
# 답변하도록 제약하여 회의/강의 녹취 기반 Q&A UX를 제공한다.

from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI

from schemas.rag import RetrievedSource
from settings import get_settings


# 시스템 프롬프트 — 한국어 회의/강의 녹취 기반 Q&A 역할 정의.
# 제공된 청크에 없는 내용은 지어내지 않고 "해당 내용이 없습니다"로 답하도록 유도해 hallucination을 방지한다.
SYSTEM_PROMPT = (
    "당신은 한국어 강의 자료 기반 질의응답 어시스턴트입니다. "
    "제공된 source에 담긴 내용만을 근거로 사용자 질문에 답하세요. "
    "질문이 강의의 주제나 개요를 묻는 경우 source의 title, topic, keywords, snippet을 종합해 답하세요. "
    "source에 없는 내용은 추측하거나 지어내지 말고, "
    "'제공된 강의 자료에는 해당 내용이 없습니다.'라고 명확히 답하세요. "
    "답변은 한국어로 핵심만 3~5문장 이내로 작성하세요."
)


class RagResponseService:
    # RagResponseService 존재 이유:
    # 검색된 parent chunk를 LLM에 전달할 context 문자열로 가공하고,
    # OpenAI chat completion 호출을 캡슐화하는 단일 책임을 가진다.
    # 라우터는 "질의 + 청크 → answer 문자열" 변환을 이 서비스 단일 호출로 이용한다.

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OPENAI_API_KEY is not configured.",
            )
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_summary_model

    async def generate(
        self,
        query: str,
        sources: list[RetrievedSource],
    ) -> str:
        """
        기능 요약: 사용자 질의와 검색된 source 목록으로 LLM 답변을 생성한다.

        기능 흐름:
            1. 검색 결과가 없으면 LLM 호출 없이 안내 메시지 조기 반환 (불필요한 API 비용 절감)
            2. _build_context(sources) → source 목록을 번호/메타데이터 포함 context 문자열로 가공
            3. OpenAI chat completion 호출 (system: 역할 제약, user: context + 질문)
            4. 응답 텍스트 추출 및 공백 검증 후 반환

        파라미터:
            query: 사용자 자연어 질의 (예: "다음 출시 일정 논의했던 내용")
            sources: RagQueryService가 검색한 근거 source 목록 (score 내림차순, 최대 top_k개)

        반환:
            LLM이 생성한 한국어 답변 문자열
        """
        # 1. 검색 결과 없음 — LLM 호출 없이 안내 메시지 반환
        if not sources:
            return "제공된 강의 자료에는 해당 내용이 없습니다."

        # 2. source 목록을 LLM context 문자열로 가공
        context = self._build_context(sources)

        # 3. chat completion 호출 — 제공된 context 범위 내 답변 생성
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"다음은 강의 자료에서 검색된 source입니다.\n\n{context}\n\n"
                            f"위 source 내용을 근거로 다음 질문에 답하세요.\n질문: {query}"
                        ),
                    },
                ],
            )
        except APIError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="RAG answer provider failed.",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="RAG answer generation failed.",
            ) from exc

        # 4. 응답 텍스트 추출 및 공백 검증
        answer = response.choices[0].message.content
        if not answer or not answer.strip():
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="RAG answer provider returned an empty response.",
            )

        return answer.strip()

   
    def _build_context(self, sources: list[RetrievedSource]) -> str:
     
        blocks: list[str] = []

        for index, source in enumerate(sources, start=1):
            # enumerate 는 lis를 index와 함께 리턴
            # 0번부터 시작하기 때문에 1부터 시작한다고 명시한다.

            meta_parts: list[str] = []
            meta_parts.append(f"source_type: {source.source_type}")
            meta_parts.append(f"title: {source.title}")
            topic = source.metadata.get("topic")
            keywords = source.metadata.get("keywords")
            if topic:
                meta_parts.append(f"topic: {topic}")
            if isinstance(keywords, list) and keywords:
                meta_parts.append(f"keywords: {', '.join(str(keyword) for keyword in keywords)}")
            meta_line = " | ".join(meta_parts)

            header = f"[source {index}]"
            if meta_line:
                header = f"{header}\n{meta_line}"

            blocks.append(f"{header}\n{source.snippet}")
            # ex)
            #[source 1]
            # topic: 출시 일정 | keywords: 베타, 릴리스
            # 다음 베타는 3월에 예정되어 있습니다...
        return "\n\n".join(blocks)
