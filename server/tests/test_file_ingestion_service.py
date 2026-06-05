from uuid import uuid4
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from schemas.rag import SegmentCreate
from services.audio.transcript_ingestion_service import TranscriptIngestionResult
from services.files.document_text_extraction_service import DocumentTextExtractionResult
from services.files.file_ingestion_service import FileIngestionService
from services.files.upload_storage_service import StoredUpload


class FakeUploadFile:
    def __init__(self, filename: str, content_type: str | None = None) -> None:
        self.filename = filename
        self.content_type = content_type


class FakeTranscriptIngestionService:
    def __init__(self) -> None:
        self.upload_calls = []
        self.segment_calls = []

    async def ingest_upload(self, **kwargs):
        self.upload_calls.append(kwargs)
        return TranscriptIngestionResult(
            transcript_id=uuid4(),
            transcript="음성 전사",
            duration_seconds=5.0,
            stt_model="whisper-1",
            segment_count=1,
            chunk_count=1,
            processing_seconds=0.1,
        )

    async def ingest_from_segments(self, **kwargs):
        self.segment_calls.append(kwargs)
        return TranscriptIngestionResult(
            transcript_id=uuid4(),
            transcript="문서 텍스트",
            duration_seconds=1.0,
            stt_model="",
            segment_count=1,
            chunk_count=1,
            processing_seconds=0.1,
        )


class FakeDocumentTextExtractionService:
    async def extract_upload(self, file, file_name):
        return DocumentTextExtractionResult(
            text="문서 텍스트",
            source_type="pdf",
            segments=[
                SegmentCreate(
                    segment_index=0,
                    start_seconds=0.0,
                    end_seconds=1.0,
                    text="문서 텍스트",
                    source_type="pdf",
                    source_page_start=1,
                    source_page_end=1,
                )
            ],
        )


class FakeUploadStorageService:
    def __init__(self) -> None:
        self.calls = []

    async def save_upload(self, file, file_name, user_id):
        self.calls.append((file, file_name, user_id))
        return StoredUpload(
            uri=f"/uploads/test/{file_name}",
            path="unused",
            original_filename=file_name,
        )


class FakeWorkItemRepository:
    def __init__(self, folder_exists: bool = True) -> None:
        self.folder_exists = folder_exists
        self.calls = []

    async def get_folder_by_id(self, folder_id, user_id):
        self.calls.append((folder_id, user_id))
        if not self.folder_exists:
            return None
        return SimpleNamespace(id=folder_id)


@pytest.mark.asyncio
async def test_file_ingestion_routes_audio_without_languages() -> None:
    transcript_service = FakeTranscriptIngestionService()
    service = FileIngestionService(
        repository=object(),
        transcript_ingestion_service=transcript_service,
        document_text_extraction_service=FakeDocumentTextExtractionService(),
        upload_storage_service=FakeUploadStorageService(),
    )

    result = await service.ingest_upload(
        file=FakeUploadFile("meeting.mp3", "audio/mpeg"),
        file_name=None,
        user_id=uuid4(),
    )

    assert result.source_type == "audio"
    assert result.file_uri == "/uploads/test/meeting.mp3"
    assert transcript_service.upload_calls[0]["file_name"] == "meeting.mp3"
    assert transcript_service.upload_calls[0]["file_uri"] == "/uploads/test/meeting.mp3"
    assert "languages" not in transcript_service.upload_calls[0]
    assert transcript_service.upload_calls[0]["folder_id"] is None


@pytest.mark.asyncio
async def test_file_ingestion_infers_mp3_extension_from_content_type() -> None:
    transcript_service = FakeTranscriptIngestionService()
    service = FileIngestionService(
        repository=object(),
        transcript_ingestion_service=transcript_service,
        document_text_extraction_service=FakeDocumentTextExtractionService(),
        upload_storage_service=FakeUploadStorageService(),
    )

    result = await service.ingest_upload(
        file=FakeUploadFile("blob", "audio/mpeg"),
        file_name=None,
        user_id=uuid4(),
    )

    assert result.source_type == "audio"
    assert result.file_uri == "/uploads/test/blob.mp3"
    assert transcript_service.upload_calls[0]["file_name"] == "blob.mp3"
    assert transcript_service.upload_calls[0]["file_uri"] == "/uploads/test/blob.mp3"
    assert transcript_service.upload_calls[0]["folder_id"] is None


@pytest.mark.asyncio
async def test_file_ingestion_routes_document_to_segment_ingestion() -> None:
    transcript_service = FakeTranscriptIngestionService()
    service = FileIngestionService(
        repository=object(),
        transcript_ingestion_service=transcript_service,
        document_text_extraction_service=FakeDocumentTextExtractionService(),
        upload_storage_service=FakeUploadStorageService(),
    )

    result = await service.ingest_upload(
        file=FakeUploadFile("lecture.pdf", "application/pdf"),
        file_name="lecture.pdf",
        user_id=uuid4(),
    )

    assert result.source_type == "document"
    assert result.file_uri == "/uploads/test/lecture.pdf"
    call = transcript_service.segment_calls[0]
    assert call["source_uri"] == "/uploads/test/lecture.pdf"
    assert call["original_filename"] == "lecture.pdf"
    assert call["source_type"] == "pdf"
    assert call["folder_id"] is None
    assert call["segments"][0].source_page_start == 1


@pytest.mark.asyncio
async def test_file_ingestion_validates_folder_before_document_upload() -> None:
    user_id = uuid4()
    folder_id = uuid4()
    transcript_service = FakeTranscriptIngestionService()
    storage_service = FakeUploadStorageService()
    work_item_repository = FakeWorkItemRepository(folder_exists=True)
    service = FileIngestionService(
        repository=object(),
        work_item_repository=work_item_repository,
        transcript_ingestion_service=transcript_service,
        document_text_extraction_service=FakeDocumentTextExtractionService(),
        upload_storage_service=storage_service,
    )

    result = await service.ingest_upload(
        file=FakeUploadFile("lecture.pdf", "application/pdf"),
        file_name="lecture.pdf",
        user_id=user_id,
        folder_id=folder_id,
    )

    assert result.folder_id == folder_id
    assert work_item_repository.calls == [(folder_id, user_id)]
    assert storage_service.calls[0][1] == "lecture.pdf"
    assert transcript_service.segment_calls[0]["folder_id"] == folder_id


@pytest.mark.asyncio
async def test_file_ingestion_rejects_invalid_folder_before_saving_file() -> None:
    user_id = uuid4()
    folder_id = uuid4()
    transcript_service = FakeTranscriptIngestionService()
    storage_service = FakeUploadStorageService()
    work_item_repository = FakeWorkItemRepository(folder_exists=False)
    service = FileIngestionService(
        repository=object(),
        work_item_repository=work_item_repository,
        transcript_ingestion_service=transcript_service,
        document_text_extraction_service=FakeDocumentTextExtractionService(),
        upload_storage_service=storage_service,
    )

    with pytest.raises(HTTPException) as exc:
        await service.ingest_upload(
            file=FakeUploadFile("lecture.pdf", "application/pdf"),
            file_name="lecture.pdf",
            user_id=user_id,
            folder_id=folder_id,
        )

    assert exc.value.status_code == 404
    assert storage_service.calls == []
    assert transcript_service.segment_calls == []


@pytest.mark.asyncio
async def test_file_ingestion_rejects_unsupported_extension() -> None:
    service = FileIngestionService(
        repository=object(),
        transcript_ingestion_service=FakeTranscriptIngestionService(),
        document_text_extraction_service=FakeDocumentTextExtractionService(),
        upload_storage_service=FakeUploadStorageService(),
    )

    with pytest.raises(HTTPException) as exc:
        await service.ingest_upload(
            file=FakeUploadFile("note.txt", "text/plain"),
            file_name=None,
            user_id=uuid4(),
        )

    assert exc.value.status_code == 400
