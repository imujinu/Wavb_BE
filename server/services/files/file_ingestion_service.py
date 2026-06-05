from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, UploadFile, status

from repositories.rag_repository import RagRepository
from repositories.work_item_repository import WorkItemRepository
from schemas.rag import TranscriptCreate
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
    folder_id: UUID | None = None


class FileIngestionService:
    """
    기능 요약: 업로드 파일을 종류별 ingestion 경로로 보내고 공통 응답 모델을 만든다.

    기능 흐름:
        1. 파일명 확장자로 audio/document 타입을 판별한다.
        2. 원본 파일을 로컬 저장소에 저장한다.
        3. transcript 메타데이터만 uploaded 상태로 생성하고 비용 발생 처리는 process API로 미룬다.

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
        work_item_repository: WorkItemRepository | None = None,
        transcript_ingestion_service: TranscriptIngestionService | None = None,
        document_text_extraction_service: DocumentTextExtractionService | None = None,
        upload_storage_service: UploadStorageService | None = None,
    ) -> None:
        self._repository = repository
        self._work_item_repository = work_item_repository
        self._transcript_ingestion_service = transcript_ingestion_service
        self._document_text_extraction_service = document_text_extraction_service
        self._upload_storage_service = upload_storage_service or UploadStorageService()

    async def ingest_upload(
        self,
        file: UploadFile,
        file_name: str | None,
        user_id: UUID | None,
        folder_id: UUID | None = None,
    ) -> FileIngestionResult:
        await self._validate_folder(folder_id, user_id)
        resolved_file_name = self._resolve_file_name(file, file_name)
        suffix = Path(resolved_file_name).suffix.lower()

        if suffix in self.AUDIO_EXTENSIONS:
            source_type = "audio"
            stored_upload = await self._upload_storage_service.save_upload(
                file,
                resolved_file_name,
                user_id,
            )
            transcript_id = await self._repository.create_transcript(
                self._to_transcript_create(
                    user_id=user_id,
                    folder_id=folder_id,
                    file_name=resolved_file_name,
                    file_uri=stored_upload.uri,
                    mime_type=file.content_type,
                    source_type=source_type,
                )
            )
            return FileIngestionResult(
                transcript_id=transcript_id,
                source_type=source_type,
                file_uri=stored_upload.uri,
                folder_id=folder_id,
                transcript="",
                segment_count=0,
                chunk_count=0,
                status="uploaded",
            )

        if suffix in self.DOCUMENT_EXTENSIONS:
            source_type = "pdf" if suffix == ".pdf" else "ppt"
            stored_upload = await self._upload_storage_service.save_upload(
                file,
                resolved_file_name,
                user_id,
            )
            transcript_id = await self._repository.create_transcript(
                self._to_transcript_create(
                    user_id=user_id,
                    folder_id=folder_id,
                    file_name=resolved_file_name,
                    file_uri=stored_upload.uri,
                    mime_type=file.content_type,
                    source_type=source_type,
                )
            )
            return FileIngestionResult(
                transcript_id=transcript_id,
                source_type=source_type,
                file_uri=stored_upload.uri,
                folder_id=folder_id,
                transcript="",
                segment_count=0,
                chunk_count=0,
                status="uploaded",
            )

        allowed = ", ".join(sorted(self.AUDIO_EXTENSIONS | self.DOCUMENT_EXTENSIONS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type. Allowed extensions: {allowed}",
        )

    def _to_transcript_create(
        self,
        user_id: UUID | None,
        folder_id: UUID | None,
        file_name: str,
        file_uri: str,
        mime_type: str | None,
        source_type: str,
    ) -> TranscriptCreate:
        """
        기능 요약: 업로드 저장 전용 transcript 생성 모델을 만든다.

        기능 흐름:
            1. 파일명 stem을 title로 사용한다.
            2. 원본 파일 URI와 메타데이터를 저장한다.
            3. content/index 상태는 pending으로 두어 사용자 처리 요청 전에는 비용이 발생하지 않게 한다.

        파라미터:
            source_type: audio/pdf/ppt 중 하나.
            file_uri: UploadStorageService가 만든 /uploads/... URI.
        """
        return TranscriptCreate(
            user_id=user_id,
            folder_id=folder_id,
            title=Path(file_name).stem,
            source_audio_uri=file_uri,
            original_filename=file_name,
            mime_type=mime_type,
            status="uploaded",
            source_type=source_type,
            content_status="pending",
            index_status="pending",
        )

    async def _validate_folder(
        self,
        folder_id: UUID | None,
        user_id: UUID | None,
    ) -> None:
        """
        기능 요약: 업로드 대상 폴더가 인증 사용자 소유인지 확인한다.

        기능 흐름:
            1. folder_id가 없으면 루트 업로드로 보고 검증을 건너뛴다.
            2. folder_id가 있으면 사용자 id와 폴더 repository가 모두 있는지 확인한다.
            3. get_folder_by_id(folder_id, user_id)로 소유 폴더 존재 여부를 검증한다.

        파라미터:
            folder_id: 업로드 대상 폴더 UUID. None이면 루트 파일로 저장한다.
            user_id: JWT에서 얻은 인증 사용자 UUID.
        """
        if folder_id is None:
            return
        if user_id is None or self._work_item_repository is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found.",
            )

        folder = await self._work_item_repository.get_folder_by_id(folder_id, user_id)
        if folder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found.",
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
