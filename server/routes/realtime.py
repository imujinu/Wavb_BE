import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from db.connection import DatabaseConnection, get_connection
from dependencies.auth import get_current_user
from repositories.rag_repository import RagRepository
from schemas.auth import CurrentUser
from schemas.realtime import RealtimeSaveRequest, RealtimeSaveResponse, RealtimeSummaryEvent
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.realtime.provider_factory import create_stt_provider
from services.realtime.summary_buffer import RealtimeSummaryBuffer

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
        """Deepgram → 모바일: 전사 이벤트를 JSON으로 전달하고, 임계값 도달 시 요약을 생성한다."""
        buffer = RealtimeSummaryBuffer(threshold_seconds=25.0)
        segment_index = 0
        # GC 방지용 강한 참조: 이벤트 루프는 태스크에 약한 참조만 유지하므로
        # GC가 실행되면 완료 전에 태스크가 소멸될 수 있다. set으로 강한 참조를 유지한다.
        _background_tasks: set[asyncio.Task] = set()

        try:
            async for event in provider.transcript_events():
                # 1. 전사 이벤트를 프론트에 즉시 전달
                await websocket.send_json(asdict(event))

                # 2. is_final 시점에만 버퍼에 누적하고 임계값을 체크한다.
                # Deepgram interim 결과는 누적이 아니라 대체(replacement)다.
                # 같은 발화에 대해 "안녕" → "안녕하세요"(final)처럼 오므로,
                # interim을 버퍼에 쌓으면 중복 텍스트로 요약이 망가진다.
                if event.is_final:
                    if event.text:
                        buffer.add(event.text)
                    if buffer.should_flush():
                        task = asyncio.create_task(
                            _send_summary(websocket, buffer, segment_index)
                        )
                        _background_tasks.add(task)
                        # 태스크 완료 시 set에서 제거하여 메모리 누수 방지
                        task.add_done_callback(_background_tasks.discard)
                        segment_index += 1
        except WebSocketDisconnect:
            pass
        except Exception:
            await websocket.send_json({
                "type": "error",
                "message": "전사 서비스에 연결할 수 없습니다.",
            })

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


async def _send_summary(
    websocket: WebSocket,
    buffer: RealtimeSummaryBuffer,
    segment_index: int,
) -> None:
    """
    기능 요약: 버퍼를 flush하고 summary 이벤트를 WebSocket으로 전송한다.
    — forward_transcripts()를 블로킹하지 않도록 별도 태스크로 실행된다.

    기능 흐름:
        1. buffer.flush_with_summary()로 GPT 요약 생성 및 버퍼 초기화
        2. RealtimeSummaryEvent 생성 후 JSON 직렬화하여 전송
        3. 실패 시 에러 메시지 전송 (스트림 중단 없음)

    파라미터:
        websocket: 클라이언트 WebSocket 연결
        buffer: 요약 대상 버퍼 인스턴스
        segment_index: 몇 번째 구간인지 (0부터)
    """
    try:
        full_text, summary = await buffer.flush_with_summary()
        event = RealtimeSummaryEvent(
            summary=summary,
            full_text=full_text,
            segment_index=segment_index,
        )
        await websocket.send_json(event.model_dump())
    except (WebSocketDisconnect, RuntimeError):
        # 클라이언트가 이미 연결을 끊은 경우 — 정상 케이스, 무시
        return
    except Exception:
        try:
            await websocket.send_json({
                "type": "error",
                "message": "요약 생성에 실패했습니다.",
            })
        except (WebSocketDisconnect, RuntimeError):
            # 에러 전송 중에도 연결이 끊긴 경우 — 정상 케이스, 무시
            return


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
