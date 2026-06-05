import pytest
from fastapi import HTTPException

from schemas.rag import SegmentCreate
from services.files.document_text_extraction_service import DocumentTextExtractionService


class FakeUploadFile:
    def __init__(self, content: bytes = b"content") -> None:
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._content):
            return b""
        if size < 0:
            size = len(self._content) - self._offset
        chunk = self._content[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


@pytest.mark.asyncio
async def test_extract_upload_returns_pdf_segments(monkeypatch) -> None:
    service = DocumentTextExtractionService()

    async def fake_extract_pdf(_):
        return [
            SegmentCreate(
                segment_index=0,
                start_seconds=0.0,
                end_seconds=1.0,
                text="첫 페이지",
                source_type="pdf",
                source_page_start=1,
                source_page_end=1,
            )
        ]

    monkeypatch.setattr(service, "_extract_pdf", fake_extract_pdf)

    result = await service.extract_upload(FakeUploadFile(), "lecture.pdf")

    assert result.source_type == "pdf"
    assert result.text == "첫 페이지"
    assert result.segments[0].source_page_start == 1
    assert result.segments[0].source_type == "pdf"


@pytest.mark.asyncio
async def test_extract_upload_returns_pptx_segments(monkeypatch) -> None:
    service = DocumentTextExtractionService()

    async def fake_extract_pptx(_):
        return [
            SegmentCreate(
                segment_index=0,
                start_seconds=0.0,
                end_seconds=1.0,
                text="첫 슬라이드",
                source_type="ppt",
                source_slide_start=1,
                source_slide_end=1,
            )
        ]

    monkeypatch.setattr(service, "_extract_pptx", fake_extract_pptx)

    result = await service.extract_upload(FakeUploadFile(), "lecture.pptx")

    assert result.source_type == "ppt"
    assert result.text == "첫 슬라이드"
    assert result.segments[0].source_slide_start == 1
    assert result.segments[0].source_type == "ppt"


@pytest.mark.asyncio
async def test_extract_upload_rejects_empty_text(monkeypatch) -> None:
    service = DocumentTextExtractionService()

    async def fake_extract_pdf(_):
        return []

    monkeypatch.setattr(service, "_extract_pdf", fake_extract_pdf)

    with pytest.raises(HTTPException) as exc:
        await service.extract_upload(FakeUploadFile(), "empty.pdf")

    assert exc.value.status_code == 422
    assert exc.value.detail == "Document text extraction result is empty."


@pytest.mark.asyncio
async def test_extract_upload_rejects_unsupported_extension() -> None:
    service = DocumentTextExtractionService()

    with pytest.raises(HTTPException) as exc:
        await service.extract_upload(FakeUploadFile(), "note.txt")

    assert exc.value.status_code == 400
