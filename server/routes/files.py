from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel

from db.connection import DatabaseConnection, get_connection
from dependencies.auth import get_current_user
from repositories.rag_repository import RagRepository
from schemas.auth import CurrentUser
from schemas.rag import UploadedFileDetail
from services.files.file_ingestion_service import FileIngestionService


router = APIRouter(prefix="/files", tags=["files"])


class FileUploadResponse(BaseModel):
    transcript_id: UUID
    source_type: str
    file_uri: str
    transcript: str
    segment_count: int
    chunk_count: int
    status: str


class UploadedFileResponse(BaseModel):
    transcript_id: UUID
    title: str | None = None
    file_uri: str
    original_filename: str | None = None
    mime_type: str | None = None
    status: str
    created_at: str | None = None


async def get_rag_repository(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[RagRepository]:
    yield RagRepository(connection)


def get_file_ingestion_service(
    repository: RagRepository = Depends(get_rag_repository),
) -> FileIngestionService:
    return FileIngestionService(repository)


@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    file_name: str | None = Form(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    ingestion_service: FileIngestionService = Depends(get_file_ingestion_service),
) -> FileUploadResponse:
    """
    기능 요약: PDF/PPT/PPTX/음성 파일을 업로드해 텍스트 추출과 RAG 인덱싱을 수행한다.

    기능 흐름:
        1. 인증 사용자를 확인한다.
        2. FileIngestionService.ingest_upload()에 업로드 파일과 선택 메타데이터를 전달한다.
        3. transcript_id와 인덱싱 통계를 공통 응답으로 반환한다.

    파라미터:
        file: 업로드 파일 (예: lecture.pdf, meeting.mp3)
        file_name: 표시/저장 파일명. 없으면 UploadFile.filename
    """
    result = await ingestion_service.ingest_upload(
        file=file,
        file_name=file_name,
        user_id=current_user.user_id,
    )
    return FileUploadResponse(
        transcript_id=result.transcript_id,
        source_type=result.source_type,
        file_uri=result.file_uri,
        transcript=result.transcript,
        segment_count=result.segment_count,
        chunk_count=result.chunk_count,
        status=result.status,
    )


@router.get("", response_model=list[UploadedFileResponse])
async def list_uploaded_files(
    current_user: CurrentUser = Depends(get_current_user),
    repository: RagRepository = Depends(get_rag_repository),
) -> list[UploadedFileResponse]:
    """
    기능 요약: 인증 사용자가 업로드한 저장 파일 목록을 최신순으로 반환한다.

    기능 흐름:
        1. JWT에서 현재 사용자 id를 얻는다.
        2. RagRepository.list_transcripts_by_user()로 본인 transcript 목록만 조회한다.
        3. DB 모델을 클라이언트 응답 모델로 직렬화한다.

    파라미터:
        current_user: Depends(get_current_user)로 주입되는 인증 사용자
    """
    files = await repository.list_transcripts_by_user(current_user.user_id)
    return [_to_uploaded_file_response(file) for file in files]


def _to_uploaded_file_response(file: UploadedFileDetail) -> UploadedFileResponse:
    return UploadedFileResponse(
        transcript_id=file.transcript_id,
        title=file.title,
        file_uri=file.file_uri,
        original_filename=file.original_filename,
        mime_type=file.mime_type,
        status=file.status,
        created_at=file.created_at.isoformat() if file.created_at else None,
    )
