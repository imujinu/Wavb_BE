import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from db.connection import DatabaseConnection, get_connection
from dependencies.auth import get_current_user
from repositories.rag_repository import RagRepository
from schemas.auth import CurrentUser
from schemas.realtime import RealtimeSaveRequest, RealtimeSaveResponse
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.realtime.provider_factory import create_stt_provider

router = APIRouter(prefix="/audio", tags=["realtime"])


async def get_rag_repository(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[RagRepository]:
    yield RagRepository(connection)


@router.websocket("/realtime/connect")
async def realtime_connect(websocket: WebSocket, token: str) -> None:
    """
    실시간 전사 WebSocket 엔드포인트.

    클라이언트 프로토콜:
    - 연결: ws://host/audio/realtime/connect?token=<JWT>
    - 전송: binary PCM16 16kHz mono 연속 스트리밍
    - 수신: {"type": "ready"} 연결 확인
            {"type": "transcript", "text": "...", "is_final": true|false}
            {"type": "error", "message": "..."}
    - 종료: 클라이언트가 WebSocket close

    asyncio.gather로 두 태스크를 동시 실행하는 이유:
    오디오 수신(forward_audio)과 전사 결과 전달(forward_transcripts)은
    독립적인 I/O 작업입니다. 순차 실행 시 한쪽이 블록되면 다른 쪽도 멈춥니다.
    """
    # JWT 검증 — 유효하지 않으면 4001 코드로 거절
    # 4001을 사용하는 이유: 표준 WebSocket close code 중 인증 실패를 나타내는 관례적 값
    try:
        await _validate_ws_token(token)
    except Exception:
        await websocket.close(code=4001)
        return

    await websocket.accept()

    provider = create_stt_provider()
    await provider.connect()
    await websocket.send_json({"type": "ready"})

    async def forward_audio() -> None:
        """모바일 → Deepgram: 바이너리 청크 수신 후 provider로 전달."""
        try:
            async for chunk in websocket.iter_bytes():
                await provider.send_audio(chunk)
        except WebSocketDisconnect:
            pass
        finally:
            # 클라이언트 연결 종료 시 provider도 정상 종료
            await provider.disconnect()

    async def forward_transcripts() -> None:
        """Deepgram → 모바일: 전사 이벤트를 JSON으로 전달."""
        try:
            async for event in provider.transcript_events():
                await websocket.send_json(asdict(event))
        except WebSocketDisconnect:
            pass

    await asyncio.gather(forward_audio(), forward_transcripts())


async def _validate_ws_token(token: str) -> dict:
    """
    WebSocket query parameter로 전달된 JWT를 검증합니다.

    query parameter를 사용하는 이유:
    WebSocket 핸드셰이크는 커스텀 HTTP 헤더를 브라우저/앱에서 설정하기
    어려우므로 JWT를 query parameter로 전달하는 것이 일반적입니다.
    """
    from jose import JWTError, jwt
    from settings import get_settings

    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
        return payload
    except JWTError:
        raise ValueError("유효하지 않은 토큰")


@router.post("/transcripts/realtime", response_model=RealtimeSaveResponse)
async def save_realtime_transcript(
    body: RealtimeSaveRequest,
    current_user: CurrentUser = Depends(get_current_user),
    repository: RagRepository = Depends(get_rag_repository),
) -> RealtimeSaveResponse:
    """
    실시간 녹음 세션 종료 후 전체 전사 결과를 DB에 저장합니다.
    TranscriptIngestionService를 통해 세그먼트 → 청크 → 임베딩 파이프라인을 실행합니다.
    """
    ingestion_service = TranscriptIngestionService(repository=repository)
    result = await ingestion_service.ingest_realtime_segments(
        domain_type=body.domain_type,
        title=body.title,
        duration_seconds=body.duration_seconds,
        segments=[s.model_dump() for s in body.segments],
        user_id=current_user.user_id,
    )
    return RealtimeSaveResponse(
        transcript_id=str(result.transcript_id),
        segment_count=result.segment_count,
    )
