from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from dependencies.auth import get_current_user
from main import app
from routes import audio
from schemas.auth import CurrentUser
from schemas.rag import SummaryDocumentDetail, TranscriptDetail


client = TestClient(app)

FAKE_USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def fake_current_user() -> CurrentUser:
    return CurrentUser(user_id=FAKE_USER_ID, email="test@example.com")


# 라우트 테스트용 가짜 서비스 — 실제 LLM/글꼴 의존 없이 결정적으로 동작한다.
class FakeSummaryService:
    async def summarize_for_template(self, transcript_text, template, title=None):
        # 템플릿 섹션 key에 더미 값을 채워 반환
        return {section.key: "요약" for section in template.sections}


class FakePdfService:
    def render(self, template, summary_payload, header=None):
        return b"%PDF-fake-pdf-bytes"


def _make_transcript(full_text: str | None) -> TranscriptDetail:
    return TranscriptDetail(
        id=uuid4(),
        user_id=FAKE_USER_ID,
        title="주간 회의",
        full_text=full_text,
        summary=None,
        duration_seconds=12.3,
        language="ko",
        status="completed",
    )


def _override(repository) -> None:
    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[audio.get_rag_repository] = lambda: repository
    app.dependency_overrides[audio.get_templated_summary_service] = lambda: FakeSummaryService()
    app.dependency_overrides[audio.get_summary_pdf_service] = lambda: FakePdfService()


def test_list_summary_templates_returns_registry() -> None:
    response = client.get("/audio/summary-templates")

    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 1
    ids = {item["id"] for item in body}
    assert "meeting_weekly" in ids


def test_create_summary_pdf_rejects_unknown_template() -> None:
    class FakeRepo:
        async def get_transcript_by_id(self, transcript_id, user_id=None):
            raise AssertionError("should not be reached")

    _override(FakeRepo())
    try:
        response = client.post(
            f"/audio/transcripts/{uuid4()}/summary-pdf",
            json={"template_id": "no_such_template"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "Summary template not found."


def test_create_summary_pdf_returns_404_for_missing_transcript() -> None:
    class FakeRepo:
        async def get_transcript_by_id(self, transcript_id, user_id=None):
            return None

    _override(FakeRepo())
    try:
        response = client.post(
            f"/audio/transcripts/{uuid4()}/summary-pdf",
            json={"template_id": "meeting_weekly"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "Transcript not found."


def test_create_summary_pdf_returns_409_for_empty_text() -> None:
    class FakeRepo:
        async def get_transcript_by_id(self, transcript_id, user_id=None):
            return _make_transcript(full_text="   ")

    _override(FakeRepo())
    try:
        response = client.post(
            f"/audio/transcripts/{uuid4()}/summary-pdf",
            json={"template_id": "meeting_weekly"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409


def test_create_summary_pdf_success_returns_pdf_with_document_header() -> None:
    document_id = uuid4()

    class FakeRepo:
        async def get_transcript_by_id(self, transcript_id, user_id=None):
            return _make_transcript(full_text="회의 원문 내용입니다.")

        async def insert_summary_document(self, document):
            # template_id/payload가 올바르게 전달됐는지 검증
            assert document.template_id == "meeting_weekly"
            assert isinstance(document.payload, dict)
            return document_id

    _override(FakeRepo())
    try:
        response = client.post(
            f"/audio/transcripts/{uuid4()}/summary-pdf",
            json={"template_id": "meeting_weekly"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-summary-document-id"] == str(document_id)
    assert response.content == b"%PDF-fake-pdf-bytes"


def test_update_summary_pdf_returns_404_for_missing_document() -> None:
    class FakeRepo:
        async def get_summary_document_by_id(self, document_id, user_id=None):
            return None

    _override(FakeRepo())
    try:
        response = client.put(
            f"/audio/summary-documents/{uuid4()}",
            json={"payload": {"overview": "수정"}},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_update_summary_pdf_success_returns_pdf() -> None:
    doc_id = uuid4()

    class FakeRepo:
        async def get_summary_document_by_id(self, document_id, user_id=None):
            return SummaryDocumentDetail(
                id=doc_id,
                transcript_id=uuid4(),
                user_id=FAKE_USER_ID,
                template_id="meeting_weekly",
                payload={"overview": "기존"},
                model="gpt-4o-mini",
            )

        async def update_summary_document_payload(self, document_id, payload, user_id=None):
            assert payload == {"overview": "수정된 개요"}
            return True

    _override(FakeRepo())
    try:
        response = client.put(
            f"/audio/summary-documents/{doc_id}",
            json={"payload": {"overview": "수정된 개요"}},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == b"%PDF-fake-pdf-bytes"
