from collections.abc import AsyncIterator
from datetime import date
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from db.connection import DatabaseConnection, get_connection
from dependencies.auth import get_current_user
from repositories.rag_repository import RagRepository
from schemas.auth import CurrentUser
from schemas.rag import LectureSummaryResponse, SummaryDocumentCreate
from settings import get_settings
from services.summary.pdf_templates import TemplateSpec, get_template, list_templates
from services.summary.summary_pdf_service import SummaryPdfService
from services.summary.summary_service import SummaryService
from services.summary.lecture_summary_service import LectureSummaryService
from services.summary.templated_summary_service import TemplatedSummaryService
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.audio.transcription_service import TranscriptionService


router = APIRouter(prefix="/audio", tags=["audio"])

ALLOWED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".webm"}
# 허용 언어 조합: 한국어 단독, 영어 단독, 한국어+영어 혼합만 허용
ALLOWED_LANGUAGE_SETS = [{"ko"}, {"en"}, {"ko", "en"}]


class AudioSummaryResponse(BaseModel):
    transcript: str
    summary: str


# POST /audio/transcripts/{id}/summary-pdf 요청 모델 — 어떤 폼으로 생성할지 선택한다.
class SummaryPdfRequest(BaseModel):
    template_id: str


# PUT /audio/summary-documents/{id} 요청 모델 — 수정된 구조화 payload를 받는다.
class SummaryDocumentUpdateRequest(BaseModel):
    payload: dict


class AudioTranscriptResponse(BaseModel):
    transcript_id: UUID
    transcript: str
    duration_seconds: float | None
    stt_model: str
    segment_count: int
    status: str


async def get_rag_repository(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[RagRepository]:
    yield RagRepository(connection)


def validate_languages(languages: list[str]) -> None:
    if set(languages) not in ALLOWED_LANGUAGE_SETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="languages는 ['ko'], ['en'], ['ko', 'en'] 조합만 허용됩니다.",
        )


def validate_audio_file(file: UploadFile) -> None:
    filename = file.filename or ""
    suffix = f".{filename.rsplit('.', 1)[-1].lower()}" if "." in filename else ""

    if suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported audio file type. Allowed extensions: {allowed}",
        )


@router.post("/summarize", response_model=AudioSummaryResponse)
async def summarize_audio(file: UploadFile = File(...)) -> AudioSummaryResponse:
    validate_audio_file(file)

    transcription_service = TranscriptionService()
    summary_service = SummaryService()

    transcript = await transcription_service.transcribe(file)
    if not transcript.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Audio transcription result is empty.",
        )

    summary = await summary_service.summarize(transcript)
    return AudioSummaryResponse(transcript=transcript, summary=summary)


@router.post("/transcripts", response_model=AudioTranscriptResponse)
async def create_audio_transcript(
    file: UploadFile = File(...),
    file_uri: str = Form(...),
    file_name: str = Form(...),
    languages: list[str] = Form(...),
    # user_id는 클라이언트 입력 대신 JWT 토큰에서 추출한 인증 사용자로 대체
    current_user: CurrentUser = Depends(get_current_user),
    repository: RagRepository = Depends(get_rag_repository),
) -> AudioTranscriptResponse:
    validate_audio_file(file)
    validate_languages(languages)

    ingestion_service = TranscriptIngestionService(repository)
    # 1. 인증된 사용자의 user_id를 토큰에서 주입하여 인제스션 실행
    result = await ingestion_service.ingest_upload(
        file=file,
        file_uri=file_uri,
        file_name=file_name,
        languages=languages,
        user_id=current_user.user_id,
    )
    return AudioTranscriptResponse(
        transcript_id=result.transcript_id,
        transcript=result.transcript,
        duration_seconds=result.duration_seconds,
        stt_model=result.stt_model,
        segment_count=result.segment_count,
        status="completed",
    )


# TemplatedSummaryService 인스턴스를 생성하는 의존성.
# 필요성: 요약 서비스는 DB 의존성이 없으므로 단순 생성만 담당하는 DI 함수로 분리해 라우트 결합을 낮춘다.
def get_templated_summary_service() -> TemplatedSummaryService:
    return TemplatedSummaryService()


# SummaryPdfService 인스턴스를 생성하는 의존성.
# 필요성: PDF 렌더 서비스를 DI로 분리해 테스트에서 override 가능하게 한다.
def get_summary_pdf_service() -> SummaryPdfService:
    return SummaryPdfService()


# LectureSummaryService 인스턴스를 생성하는 의존성.
# 필요성: transcript/chunk 저장소를 사용하는 새 강의 요약 데이터 API를 테스트에서 쉽게 교체한다.
def get_lecture_summary_service(
    repository: RagRepository = Depends(get_rag_repository),
) -> LectureSummaryService:
    return LectureSummaryService(repository)


# PDF 바이트를 다운로드 응답(StreamingResponse)으로 변환하는 공통 헬퍼.
# 필요성: 생성/재렌더 두 엔드포인트가 동일한 응답 포맷을 사용하므로 한 곳으로 모은다.
# 파라미터:
#   pdf_bytes: 렌더된 PDF 바이트
#   filename: 다운로드 파일명 (예: "summary_meeting_weekly.pdf")
#   document_id: 생성 시에만 전달 — 응답 헤더로 문서 id를 노출해 이후 수정에 사용
def _pdf_streaming_response(
    pdf_bytes: bytes,
    filename: str,
    document_id: UUID | None = None,
) -> StreamingResponse:
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    # 생성 직후에는 문서 id를 헤더로 내려 클라이언트가 수정 요청 시 참조하도록 한다
    if document_id is not None:
        headers["X-Summary-Document-Id"] = str(document_id)
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers=headers,
    )


@router.get("/summary-templates", response_model=list[TemplateSpec])
def list_summary_templates() -> list[TemplateSpec]:
    """
    기능 요약: 사용 가능한 요약 PDF 폼(템플릿) 목록을 반환한다.

    기능 흐름:
        1. 코드 레지스트리(list_templates)에서 전체 TemplateSpec 목록을 반환
        (추후 DB 전환 시 이 엔드포인트 시그니처는 유지하고 내부 구현만 교체)
    """
    # 1. 등록된 전체 템플릿 반환 — 앱이 폼 목록을 동적으로 렌더링한다
    return list_templates()


@router.post(
    "/transcripts/{transcript_id}/summary",
    response_model=LectureSummaryResponse,
)
async def create_lecture_summary(
    transcript_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    summary_service: LectureSummaryService = Depends(get_lecture_summary_service),
) -> LectureSummaryResponse:
    """
    기능 요약: 저장된 transcript와 chunk를 바탕으로 강의 요약 데이터 JSON을 생성하거나 기존 데이터를 반환한다.

    기능 흐름:
        1. LectureSummaryService.get_or_create_summary(...) 호출
        2. 서비스 내부에서 소유권/상태/chunk 준비 여부를 검증
        3. 기존 lecture_summaries가 있으면 재사용, 없으면 LLM 생성 후 저장

    파라미터:
        transcript_id: 요약할 transcript UUID
    """
    # 1. 인증 사용자 기준으로 강의 요약 데이터 생성/조회
    return await summary_service.get_or_create_summary(
        transcript_id,
        current_user.user_id,
    )


@router.post("/transcripts/{transcript_id}/summary-pdf")
async def create_summary_pdf(
    transcript_id: UUID,
    request: SummaryPdfRequest,
    current_user: CurrentUser = Depends(get_current_user),
    repository: RagRepository = Depends(get_rag_repository),
    summary_service: TemplatedSummaryService = Depends(get_templated_summary_service),
    pdf_service: SummaryPdfService = Depends(get_summary_pdf_service),
) -> StreamingResponse:
    """
    기능 요약: 저장된 스크립트를 선택한 템플릿으로 요약하여 PDF를 생성하고, 결과를 영속화한 뒤 다운로드한다.

    기능 흐름:
        1. get_template(template_id) → 템플릿 검증 (없으면 404)
        2. repository.get_transcript_by_id(...) → 소유 스크립트 조회 (없음/비소유 404, full_text 빈값 409)
        3. summary_service.summarize_for_template(...) → 섹션별 구조화 JSON 생성
        4. repository.insert_summary_document(...) → 수정/재렌더용 payload 영속화
        5. pdf_service.render(...) → 한글 PDF 바이트 생성
        6. _pdf_streaming_response(...) → PDF 다운로드 + X-Summary-Document-Id 헤더 반환

    파라미터:
        transcript_id: 요약할 스크립트 UUID (경로 파라미터)
        request: template_id(선택한 폼) 를 담은 요청 본문
    """
    # 1. 템플릿 검증
    template = get_template(request.template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Summary template not found.",
        )

    # 2. 소유 스크립트 조회 — 인증 사용자 소유만 허용
    transcript = await repository.get_transcript_by_id(
        transcript_id,
        current_user.user_id,
    )
    if transcript is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transcript not found.",
        )
    if not transcript.full_text or not transcript.full_text.strip():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Transcript has no text to summarize.",
        )

    # 3. 템플릿 섹션 스키마에 맞춘 구조화 요약 생성
    payload = await summary_service.summarize_for_template(
        transcript_text=transcript.full_text,
        template=template,
        title=transcript.title,
    )

    # 4. 수정→재렌더를 위해 구조화 payload를 영속화
    document_id = await repository.insert_summary_document(
        SummaryDocumentCreate(
            transcript_id=transcript_id,
            user_id=current_user.user_id,
            template_id=template.id,
            payload=payload,
            model=get_settings().summary_pdf_model,
        )
    )

    # 5. PDF 렌더 (머리말에 원본 제목/생성일 표기)
    pdf_bytes = pdf_service.render(
        template=template,
        summary_payload=payload,
        header={"title": transcript.title, "generated_at": date.today().isoformat()},
    )

    # 6. 다운로드 응답 + 문서 id 헤더
    return _pdf_streaming_response(
        pdf_bytes,
        filename=f"summary_{template.id}.pdf",
        document_id=document_id,
    )


@router.put("/summary-documents/{document_id}")
async def update_summary_pdf(
    document_id: UUID,
    request: SummaryDocumentUpdateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    repository: RagRepository = Depends(get_rag_repository),
    pdf_service: SummaryPdfService = Depends(get_summary_pdf_service),
) -> StreamingResponse:
    """
    기능 요약: 저장된 요약 문서의 payload를 수정 내용으로 갱신하고, LLM 재호출 없이 PDF만 다시 렌더한다.

    기능 흐름:
        1. repository.get_summary_document_by_id(...) → 소유 문서 조회 (없음/비소유 404)
        2. repository.update_summary_document_payload(...) → 수정 payload 영속화
        3. get_template(저장된 template_id) → 렌더 양식 복원 (없으면 409)
        4. pdf_service.render(...) → 수정 내용이 반영된 PDF 생성 후 다운로드

    파라미터:
        document_id: 수정할 요약 문서 UUID (경로 파라미터)
        request: 수정된 구조화 payload 를 담은 요청 본문
    """
    # 1. 소유 문서 조회
    document = await repository.get_summary_document_by_id(
        document_id,
        current_user.user_id,
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Summary document not found.",
        )

    # 2. 수정 payload 영속화 (LLM 재호출 없음)
    await repository.update_summary_document_payload(
        document_id,
        request.payload,
        current_user.user_id,
    )

    # 3. 저장된 template_id로 렌더 양식 복원 — 레지스트리에서 사라진 폼이면 409
    template = get_template(document.template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Summary template is no longer available.",
        )

    # 4. 수정 내용 반영 PDF 렌더 후 다운로드
    pdf_bytes = pdf_service.render(
        template=template,
        summary_payload=request.payload,
        header={"generated_at": date.today().isoformat()},
    )
    return _pdf_streaming_response(
        pdf_bytes,
        filename=f"summary_{template.id}.pdf",
    )
