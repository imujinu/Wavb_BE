from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI

from settings import get_settings


SYSTEM_PROMPT = (
    "당신은 한국어 음성 기록을 명확하게 정리하는 문서 요약 도우미입니다. "
    "원문에 없는 내용을 만들지 말고, 핵심 내용 중심으로 간결하게 요약하세요."
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

    async def summarize(self, transcript: str) -> str:
        if not transcript.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Transcript cannot be empty.",
            )

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "다음 음성 기록을 한국어로 요약해 주세요.\n"
                            "- 핵심 내용 3~5개\n"
                            "- 전체 요약 2~3문장\n"
                            "- 중요한 후속 작업이 있으면 포함\n\n"
                            f"음성 기록:\n{transcript}"
                        ),
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

        summary = response.choices[0].message.content
        if not summary or not summary.strip():
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Summary provider returned an empty response.",
            )

        return summary.strip()
