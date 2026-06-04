from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from dependencies.auth import get_current_user
from main import app
from routes import rag
from schemas.auth import CurrentUser
from schemas.rag import RetrievedSource


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
