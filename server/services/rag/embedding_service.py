import logging

from openai import AsyncOpenAI, OpenAIError

from settings import get_settings

logger = logging.getLogger(__name__)


# OpenAI embeddings API를 통해 텍스트 목록을 벡터로 변환하는 서비스.
# RAG 파이프라인에서 청크 텍스트를 pgvector에 저장하기 전 임베딩 생성에 사용된다.
class EmbeddingServiceError(Exception):
    """OpenAI embedding API 호출 실패 시 발생하는 커스텀 예외."""


class EmbeddingService:
    # EmbeddingService 존재 이유:
    # ChunkMetadataService가 토픽/키워드를 enrichment하듯,
    # EmbeddingService는 청크 텍스트를 pgvector 저장용 벡터로 변환하는 단일 책임을 가진다.
    # OpenAI client 초기화와 모델 설정을 캡슐화하여 호출자가 API 세부사항을 몰라도 되게 한다.

    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_embedding_model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        기능 요약: OpenAI embeddings API를 호출해 텍스트 목록의 벡터 임베딩을 반환한다.

        기능 흐름:
            1. 빈 입력 조기 반환 (API 호출 불필요)
            2. OpenAI embeddings API 호출 (batch 지원 — list[str] 한 번에 처리)
            3. 응답 data 목록에서 .embedding 속성 추출
            4. 벡터 목록 반환

        파라미터:
            texts: 임베딩할 텍스트 목록
                   예: ["회의에서 다음 출시 일정을 논의했다.", "역전파의 기울기 계산 원리"]
        반환:
            각 텍스트에 대응하는 1536차원 float 벡터 목록
            예: [[0.012, -0.034, ...], [0.056, 0.078, ...]]
        """
        # 1. 빈 입력은 API 호출 없이 빈 리스트 반환
        if not texts:
            return []

        try:
            # 2. OpenAI embeddings API 배치 호출
            response = await self._client.embeddings.create(
                model=self._model,
                input=texts,
            )
        except OpenAIError as exc:
            logger.error("OpenAI embedding API failed: %s", exc)
            raise EmbeddingServiceError(str(exc)) from exc

        # 3. 응답 객체에서 벡터 목록 추출 (입력 순서 보장)
        embeddings = [item.embedding for item in response.data]

        # 4. 벡터 목록 반환
        return embeddings
