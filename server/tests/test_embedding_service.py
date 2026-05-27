from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import OpenAIError

from services.embedding_service import EmbeddingService, EmbeddingServiceError


# --- 헬퍼: 임베딩 응답 객체 생성 ---

def _make_embedding_response(vectors: list[list[float]]) -> SimpleNamespace:
    """OpenAI embeddings.create() 응답 형식을 흉내내는 SimpleNamespace 생성."""
    return SimpleNamespace(
        data=[
            SimpleNamespace(embedding=vector)
            for vector in vectors
        ]
    )


# --- 헬퍼: 설정 패치 없이 EmbeddingService 직접 조립 ---

def _make_service(model: str = "text-embedding-3-small") -> tuple[EmbeddingService, AsyncMock]:
    """
    __new__로 인스턴스를 생성해 실제 settings/OpenAI 초기화를 우회하고
    mock client를 주입한 EmbeddingService 반환.
    """
    mock_create = AsyncMock()
    mock_client = MagicMock()
    mock_client.embeddings.create = mock_create

    service = EmbeddingService.__new__(EmbeddingService)
    service._client = mock_client
    service._model = model

    return service, mock_create


# --- 테스트 ---

@pytest.mark.asyncio
async def test_embed_single_text_returns_vector() -> None:
    """단일 텍스트 입력 시 대응하는 벡터 1개를 반환하는지 확인."""
    # 1536차원 벡터를 단순화해 [0.1, 0.2, ..., 0.5] 패턴으로 준비
    expected_vector = [0.1] * 512 + [0.2] * 512 + [0.5] * 512
    service, mock_create = _make_service()
    mock_create.return_value = _make_embedding_response([expected_vector])

    result = await service.embed(["hello world"])

    assert result == [expected_vector]


@pytest.mark.asyncio
async def test_embed_batch_texts_returns_multiple_vectors() -> None:
    """복수 텍스트 입력 시 각 텍스트에 대응하는 벡터 목록을 반환하는지 확인."""
    vectors = [
        [float(i)] * 1536 for i in range(3)
    ]
    service, mock_create = _make_service()
    mock_create.return_value = _make_embedding_response(vectors)

    result = await service.embed(["text1", "text2", "text3"])

    # 반환값이 3개 벡터인지 확인
    assert len(result) == 3
    # 각 항목이 list[float]인지 확인
    for vector in result:
        assert isinstance(vector, list)
        assert all(isinstance(v, float) for v in vector)


@pytest.mark.asyncio
async def test_embed_handles_openai_api_error_gracefully() -> None:
    """OpenAI API 오류 발생 시 EmbeddingServiceError를 raise하는지 확인."""
    service, mock_create = _make_service()
    mock_create.side_effect = OpenAIError("connection timeout")

    with pytest.raises(EmbeddingServiceError):
        await service.embed(["텍스트"])


@pytest.mark.asyncio
async def test_embed_uses_configured_model() -> None:
    """생성자에서 설정된 모델명이 embeddings.create() 호출 시 전달되는지 확인."""
    service, mock_create = _make_service(model="text-embedding-3-large")
    mock_create.return_value = _make_embedding_response([[0.1] * 1536])

    await service.embed(["샘플 텍스트"])

    # mock_create 호출 시 model 인자가 설정값과 일치하는지 검증
    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["model"] == "text-embedding-3-large"


@pytest.mark.asyncio
async def test_embed_empty_input_returns_empty_list_without_api_call() -> None:
    """빈 텍스트 목록 입력 시 API를 호출하지 않고 빈 리스트를 반환하는지 확인."""
    service, mock_create = _make_service()

    result = await service.embed([])

    assert result == []
    mock_create.assert_not_called()
