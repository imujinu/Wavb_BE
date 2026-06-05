from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, UploadFile, status

from repositories.rag_repository import RagRepository
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.files.document_text_extraction_service import DocumentTextExtractionService
from services.files.upload_storage_service import UploadStorageService


@dataclass(frozen=True)
class FileIngestionResult:
    transcript_id: UUID
    source_type: str
    file_uri: str
    transcript: str
    segment_count: int
    chunk_count: int
    status: str


class FileIngestionService:
    """
    기능 요약: 업로드 파일을 종류별 ingestion 경로로 보내고 공통 응답 모델을 만든다.

    기능 흐름:
        1. 파일명 확장자로 audio/document 타입을 판별한다.
        2. audio는 기존 TranscriptIngestionService.ingest_upload()에 위임한다.
        3. document는 텍스트 추출 후 ingest_from_segments()로 기존 chunk/embedding 파이프라인을 재사용한다.

    파라미터 예시:
        file: UploadFile("lecture.pdf")
        file_name: "lecture.pdf"
        user_id: 인증 사용자 UUID
    """

    AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".webm"}
    DOCUMENT_EXTENSIONS = {".pdf", ".ppt", ".pptx"}
    CONTENT_TYPE_EXTENSIONS = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/webm": ".webm",
        "application/pdf": ".pdf",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    }

    def __init__(
        self,
        repository: RagRepository,
        transcript_ingestion_service: TranscriptIngestionService | None = None,
        document_text_extraction_service: DocumentTextExtractionService | None = None,
        upload_storage_service: UploadStorageService | None = None,
    ) -> None:
        self._repository = repository
        self._transcript_ingestion_service = (
            transcript_ingestion_service or TranscriptIngestionService(repository)
        )
        self._document_text_extraction_service = (
            document_text_extraction_service or DocumentTextExtractionService()
        )
        self._upload_storage_service = upload_storage_service or UploadStorageService()

    async def ingest_upload(
        self,
        file: UploadFile,
        file_name: str | None,
        user_id: UUID | None,
    ) -> FileIngestionResult:
        resolved_file_name = self._resolve_file_name(file, file_name)
        suffix = Path(resolved_file_name).suffix.lower()

        if suffix in self.AUDIO_EXTENSIONS:
            stored_upload = await self._upload_storage_service.save_upload(
                file,
                resolved_file_name,
                user_id,
            )
            result = await self._transcript_ingestion_service.ingest_upload(
                file=file,
                file_uri=stored_upload.uri,
                file_name=resolved_file_name,
                user_id=user_id,
            )
            return FileIngestionResult(
                transcript_id=result.transcript_id,
                source_type="audio",
                file_uri=stored_upload.uri,
                transcript=result.transcript,
                segment_count=result.segment_count,
                chunk_count=result.chunk_count,
                status="completed",
            )

        if suffix in self.DOCUMENT_EXTENSIONS:
            stored_upload = await self._upload_storage_service.save_upload(
                file,
                resolved_file_name,
                user_id,
            )
            extraction = await self._document_text_extraction_service.extract_upload(
                file,
                resolved_file_name,
            )
            result = await self._transcript_ingestion_service.ingest_from_segments(
                segments=extraction.segments,
                title=Path(resolved_file_name).stem,
                duration_seconds=float(len(extraction.segments)),
                user_id=user_id,
                source_uri=stored_upload.uri,
                original_filename=resolved_file_name,
                mime_type=file.content_type,
                source_type=extraction.source_type,
            )
            return FileIngestionResult(
                transcript_id=result.transcript_id,
                source_type="document",
                file_uri=stored_upload.uri,
                transcript=result.transcript,
                segment_count=result.segment_count,
                chunk_count=result.chunk_count,
                status="completed",
            )

        allowed = ", ".join(sorted(self.AUDIO_EXTENSIONS | self.DOCUMENT_EXTENSIONS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type. Allowed extensions: {allowed}",
        )

    def _resolve_file_name(
        self,
        file: UploadFile,
        file_name: str | None,
    ) -> str:
        """
        기능 요약: 업로드 파일명에 확장자가 없을 때 content_type으로 저장용 파일명을 보완한다.

        기능 흐름:
            1. 클라이언트가 보낸 file_name을 우선 사용하고, 없으면 multipart filename을 사용한다.
            2. 파일명에 확장자가 있으면 그대로 반환한다.
            3. 확장자가 없고 content_type을 아는 경우 허용 확장자를 붙인다.

        파라미터:
            file: UploadFile(filename="blob", content_type="audio/mpeg")
            file_name: 선택 표시 파일명 (예: "recording")
        """
        base_name = (file_name or file.filename or "upload").strip() or "upload"
        if Path(base_name).suffix:
            return base_name

        inferred_suffix = self.CONTENT_TYPE_EXTENSIONS.get(
            (file.content_type or "").lower()
        )
        if inferred_suffix is None:
            return base_name
        return f"{base_name}{inferred_suffix}"
