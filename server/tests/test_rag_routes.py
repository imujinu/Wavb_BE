from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from dependencies.auth import get_current_user
from main import app
from routes import rag
from schemas.auth import CurrentUser
from schemas.rag import RetrievedSource
from services.rag.web_search_service import WebSearchProviderError


client = TestClient(app)
FAKE_USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def fake_current_user() -> CurrentUser:
    return CurrentUser(user_id=FAKE_USER_ID, email="test@example.com")


def test_rag_query_uses_authenticated_user_and_returns_sources() -> None:
    transcript_id = uuid4()

    class FakeRagQueryService:
        async def search(self, query, transcript_ids, user_id, top_k):
            assert query == "역전파가 뭐야?"
            assert transcript_ids == [transcript_id]
            assert user_id == FAKE_USER_ID
            assert top_k == 5
            return [
                RetrievedSource(
                    source_type="document",
                    title="딥러닝 강의",
                    snippet="역전파는 기울기 계산 과정입니다.",
                    transcript_id=transcript_id,
                    score=0.5,
                )
            ]

    class FakeRagResponseService:
        async def generate(self, query, sources):
            assert query == "역전파가 뭐야?"
            assert len(sources) == 1
            return "역전파는 기울기 계산 과정입니다."

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[rag.get_rag_query_service] = lambda: FakeRagQueryService()
    app.dependency_overrides[rag.get_rag_response_service] = (
        lambda: FakeRagResponseService()
    )

    try:
        response = client.post(
            "/rag/query",
            json={
                "query": "역전파가 뭐야?",
                "transcript_ids": [str(transcript_id)],
                "top_k": 5,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "역전파는 기울기 계산 과정입니다."
    assert body["warnings"] == []
    assert body["sources"][0]["source_type"] == "document"
    assert body["sources"][0]["title"] == "딥러닝 강의"
    assert body["sources"][0]["transcript_id"] == str(transcript_id)


def test_rag_query_rejects_empty_transcript_ids() -> None:
    app.dependency_overrides[get_current_user] = fake_current_user

    try:
        response = client.post(
            "/rag/query",
            json={
                "query": "질문",
                "transcript_ids": [],
                "top_k": 5,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_rag_query_allows_web_scope_without_transcript_ids() -> None:
    class FakeRagQueryService:
        async def search(self, query, transcript_ids, user_id, top_k):
            raise AssertionError("document search should not run for web scope")

    class FakeWebSearchService:
        async def search(self, query, max_results=None):
            assert query == "최신 검색"
            assert max_results == 3
            return [
                RetrievedSource(
                    source_type="web",
                    title="검색 결과",
                    snippet="웹 검색 내용",
                    url="https://example.com",
                    score=0.8,
                )
            ]

    class FakeRagResponseService:
        async def generate(self, query, sources):
            assert len(sources) == 1
            assert sources[0].source_type == "web"
            return "웹 기반 답변"

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[rag.get_rag_query_service] = lambda: FakeRagQueryService()
    app.dependency_overrides[rag.get_web_search_service] = lambda: FakeWebSearchService()
    app.dependency_overrides[rag.get_rag_response_service] = (
        lambda: FakeRagResponseService()
    )

    try:
        response = client.post(
            "/rag/query",
            json={
                "query": "최신 검색",
                "scope": "web",
                "top_k": 3,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "웹 기반 답변"
    assert body["sources"][0]["source_type"] == "web"
    assert body["sources"][0]["url"] == "https://example.com"


def test_rag_query_hybrid_returns_document_with_warning_when_web_fails() -> None:
    transcript_id = uuid4()

    class FakeRagQueryService:
        async def search(self, query, transcript_ids, user_id, top_k):
            return [
                RetrievedSource(
                    source_type="document",
                    title="문서 결과",
                    snippet="문서 내용",
                    transcript_id=transcript_id,
                    score=0.2,
                )
            ]

    class FakeWebSearchService:
        async def search(self, query, max_results=None):
            raise WebSearchProviderError("Tavily search failed.")

    class FakeRagResponseService:
        async def generate(self, query, sources):
            assert len(sources) == 1
            assert sources[0].source_type == "document"
            return "문서 기반 답변"

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[rag.get_rag_query_service] = lambda: FakeRagQueryService()
    app.dependency_overrides[rag.get_web_search_service] = lambda: FakeWebSearchService()
    app.dependency_overrides[rag.get_rag_response_service] = (
        lambda: FakeRagResponseService()
    )

    try:
        response = client.post(
            "/rag/query",
            json={
                "query": "혼합 검색",
                "scope": "hybrid",
                "transcript_ids": [str(transcript_id)],
                "top_k": 5,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "문서 기반 답변"
    assert body["warnings"] == ["Tavily search failed."]
    assert body["sources"][0]["source_type"] == "document"
