import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status
from fastapi import HTTPException

from db.connection import DatabaseConnection, get_connection
from dependencies.auth import get_current_user
from repositories.rag_repository import RagRepository
from schemas.auth import CurrentUser
from schemas.rag import SegmentCreate
from schemas.realtime import (
    RealtimeSaveRequest,
    RealtimeSaveResponse,
    RealtimeTranscriptEvent,
)
from services.realtime_transcription_service import RealtimeTranscriptionService
from services.transcript_ingestion_service import TranscriptIngestionService
from settings import get_settings
from utils import jwt_utils

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["realtime"])


async def get_rag_repository(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[RagRepository]:
    yield RagRepository(connection)


@router.websocket("/realtime")
async def realtime_transcription(
    websocket: WebSocket,
    token: str = Query(...),
) -> None:
    # 1. JWT 검증 — 실패 시 4001 코드로 종료
    try:
        jwt_utils.decode_access_token(token, get_settings())
    except HTTPException:
        await websocket.close(code=4001)
        return

    await websocket.accept()

    transcription_service = RealtimeTranscriptionService()
    chunk_index = 0
    accumulated_text = ""

    # 2. {"type":"ready"} 전송 후 클라이언트 수신 대기
    ready_event = RealtimeTranscriptEvent(type="ready")
    await websocket.send_text(ready_event.model_dump_json())

    try:
        while True:
            message = await websocket.receive()

            # 3. binary 메시지 → 오디오 청크 전사
            if "bytes" in message and message["bytes"]:
                event = await transcription_service.transcribe_chunk(
                    audio_bytes=message["bytes"],
                    chunk_index=chunk_index,
                    accumulated_text=accumulated_text,
                )
                await websocket.send_text(event.model_dump_json())

                if event.type == "transcript" and event.text:
                    accumulated_text += " " + event.text if accumulated_text else event.text

                chunk_index += 1

            # 4. text 메시지 → 제어 명령 처리
            elif "text" in message and message["text"]:
                try:
                    control = json.loads(message["text"])
                except (json.JSONDecodeError, TypeError):
                    continue

                if control.get("type") == "stop":
                    break

    except WebSocketDisconnect:
        # 클라이언트가 먼저 연결을 끊은 경우 — 정상 종료
        logger.info("WebSocket 연결 종료 (chunk %d 처리 완료)", chunk_index)


@router.post(
    "/transcripts/realtime",
    status_code=status.HTTP_201_CREATED,
    response_model=RealtimeSaveResponse,
)
async def save_realtime_transcript(
    body: RealtimeSaveRequest,
    current_user: CurrentUser = Depends(get_current_user),
    repository: RagRepository = Depends(get_rag_repository),
) -> RealtimeSaveResponse:
    # 1. RealtimeSegmentInput → SegmentCreate 변환
    segments = [
        SegmentCreate(
            segment_index=seg.segment_index,
            start_seconds=seg.start_seconds,
            end_seconds=seg.end_seconds,
            text=seg.text,
            raw_metadata={"source": "realtime"},
        )
        for seg in body.segments
    ]

    # 2. segments 기반으로 transcript 저장 및 RAG 파이프라인 실행
    ingestion_service = TranscriptIngestionService(repository=repository)
    result = await ingestion_service.ingest_from_segments(
        segments=segments,
        domain_type=body.domain_type,
        title=body.title,
        duration_seconds=body.duration_seconds,
        user_id=current_user.user_id,
    )

    return RealtimeSaveResponse(
        transcript_id=result.transcript_id,
        segment_count=result.segment_count,
    )
