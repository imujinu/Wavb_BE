import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from db.connection import DatabaseConnection, get_connection
from dependencies.auth import get_current_user
from repositories.rag_repository import RagRepository
from schemas.auth import CurrentUser
from schemas.rag import TemporarySegmentCreate, TranscriptCreate
from schemas.realtime import RealtimeSaveRequest, RealtimeSaveResponse, RealtimeSummaryEvent
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.realtime.provider_factory import create_stt_provider
from services.realtime.summary_buffer import (
    DEFAULT_REALTIME_SUMMARY_THRESHOLD_SECONDS,
    RealtimeSummaryBuffer,
    RealtimeSummarySnapshot,
)
from utils import jwt_utils

router = APIRouter(prefix="/audio", tags=["realtime"])

logger = logging.getLogger("realtime")


async def get_rag_repository(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[RagRepository]:
    yield RagRepository(connection)


@router.websocket("/realtime/connect")
async def realtime_connect(
    websocket: WebSocket,
    token: str,
    connection: DatabaseConnection = Depends(get_connection),
) -> None:
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
        current_user = await _validate_ws_token(token)
    except Exception:
        logger.warning("WS 토큰 검증 실패 — 4001로 연결 거절")
        await websocket.close(code=4001)
        return

    await websocket.accept()
    logger.info("WS 연결 수락 — 전사 세션 시작")
    repository = RagRepository(connection)
    transcript_id = await repository.create_transcript(
        TranscriptCreate(
            user_id=current_user.user_id,
            title="Realtime recording",
            source_audio_uri="realtime://recording",
            source_type="audio",
            status="uploaded",
            content_status="pending",
            index_status="pending",
            mime_type="audio/webm",
        )
    )

    provider = create_stt_provider()
    await provider.connect()
    await websocket.send_json({"type": "ready", "transcript_id": str(transcript_id)})
    logger.info("STT provider 연결 완료, ready 전송")

    async def forward_audio() -> None:
        """모바일 → Deepgram: 바이너리 청크 수신 후 provider로 전달."""
        total_bytes = 0
        chunk_count = 0
        try:
            async for chunk in websocket.iter_bytes():
                chunk_count += 1
                total_bytes += len(chunk)
                # 매 50청크마다 누적 수신량 로깅 (청크마다 찍으면 로그 폭주)
                if chunk_count % 50 == 1:
                    logger.info(
                        "오디오 수신 #%d: %d bytes (누적 %d bytes)",
                        chunk_count,
                        len(chunk),
                        total_bytes,
                    )
                await provider.send_audio(chunk)
        except WebSocketDisconnect:
            logger.info("WS 연결 종료 — 오디오 %d청크 %d bytes 수신", chunk_count, total_bytes)
        finally:
            # 클라이언트 연결 종료 시 provider도 정상 종료
            await provider.disconnect()

    async def forward_transcripts() -> None:
        """Deepgram → 모바일: 전사 이벤트를 JSON으로 전달하고, 임계값 도달 시 요약을 생성한다."""
        buffer = RealtimeSummaryBuffer(
            threshold_seconds=DEFAULT_REALTIME_SUMMARY_THRESHOLD_SECONDS
        )
        segment_index = 0
        # final transcript에 부여하는 단조 증가 인덱스.
        # FE는 이 값을 세그먼트 키로 저장하고, summary의 범위(start/end_final_index)와 매칭해
        # 해당 구간의 실시간 라인만 정확히 접는다(collapse). interim에는 부여하지 않는다.
        final_index = 0
        # GC 방지용 강한 참조: 이벤트 루프는 태스크에 약한 참조만 유지하므로
        # GC가 실행되면 완료 전에 태스크가 소멸될 수 있다. set으로 강한 참조를 유지한다.
        _background_tasks: set[asyncio.Task] = set()

        try:
            async for event in provider.transcript_events():
                # 전사 수신 로그 — is_final 여부와 텍스트를 함께 기록
                logger.info(
                    "전사 수신 [%s] %r",
                    "final" if event.is_final else "interim",
                    event.text,
                )
                # 1. 전사 이벤트를 프론트에 즉시 전달 (final이면 final_index 동봉)
                payload = asdict(event)
                if event.is_final:
                    payload["final_index"] = final_index
                await websocket.send_json(payload)

                # 2. is_final 시점에만 버퍼에 누적하고 임계값을 체크한다.
                # Deepgram interim 결과는 누적이 아니라 대체(replacement)다.
                # 같은 발화에 대해 "안녕" → "안녕하세요"(final)처럼 오므로,
                # interim을 버퍼에 쌓으면 중복 텍스트로 요약이 망가진다.
                if event.is_final:
                    if event.text:
                        await repository.insert_temporary_segment(
                            transcript_id,
                            TemporarySegmentCreate(
                                segment_index=final_index,
                                start_seconds=None,
                                end_seconds=None,
                                text=event.text,
                                raw_metadata={
                                    "provider": "deepgram",
                                    "source": "realtime",
                                },
                            ),
                        )
                        buffer.add(event.text, final_index)
                    if buffer.should_flush():
                        snapshot = buffer.drain()
                        if not snapshot.is_empty:
                            task = asyncio.create_task(
                                _send_summary(websocket, buffer, snapshot, segment_index)
                            )
                            _background_tasks.add(task)
                            # 태스크 완료 시 set에서 제거하여 메모리 누수 방지
                            task.add_done_callback(_background_tasks.discard)
                            segment_index += 1
                    # final 단위로만 인덱스 증가 (빈 텍스트 final 포함 — FE는 빈 final을 저장 안 하므로 무해)
                    final_index += 1
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("전사 스트림 처리 중 오류 발생")
            await websocket.send_json({
                "type": "error",
                "message": "전사 서비스에 연결할 수 없습니다.",
            })
        finally:
            temporary_segments = await repository.list_temporary_segments(transcript_id)
            temporary_text = " ".join(
                segment.text for segment in temporary_segments if segment.text.strip()
            )
            if temporary_text:
                await repository.update_temporary_text(transcript_id, temporary_text)

    await asyncio.gather(forward_audio(), forward_transcripts())


async def _validate_ws_token(token: str) -> CurrentUser:
    """
    WebSocket query parameter로 전달된 JWT를 검증합니다.

    query parameter를 사용하는 이유:
    WebSocket 핸드셰이크는 커스텀 HTTP 헤더를 브라우저/앱에서 설정하기
    어려우므로 JWT를 query parameter로 전달하는 것이 일반적입니다.
    """
    from settings import get_settings

    return jwt_utils.decode_access_token(token, get_settings())


async def _send_summary(
    websocket: WebSocket,
    buffer: RealtimeSummaryBuffer,
    snapshot: RealtimeSummarySnapshot,
    segment_index: int,
) -> None:
    """
    기능 요약: 버퍼를 flush하고 summary 이벤트를 WebSocket으로 전송한다.
    — forward_transcripts()를 블로킹하지 않도록 별도 태스크로 실행된다.

    기능 흐름:
        1. buffer.flush_with_summary()로 요약·키워드 생성 및 버퍼 초기화 (final 범위 동반 반환)
        2. RealtimeSummaryEvent 생성 후 JSON 직렬화하여 전송
        3. 실패 시 에러 메시지 전송 (스트림 중단 없음)

    파라미터:
        websocket: 클라이언트 WebSocket 연결
        buffer: 요약 대상 버퍼 인스턴스
        segment_index: 몇 번째 구간인지 (0부터)
    """
    try:
        summary, keywords = await buffer.summarize_snapshot(snapshot)
        event = RealtimeSummaryEvent(
            summary=summary,
            full_text=snapshot.full_text,
            segment_index=segment_index,
            start_final_index=snapshot.start_final_index,
            end_final_index=snapshot.end_final_index,
            keywords=keywords,
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


@router.post(
    "/transcripts/realtime",
    response_model=RealtimeSaveResponse,
    summary="실시간 녹음 세션의 최종 세그먼트를 저장하고 RAG 인덱싱을 수행한다.",
)
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
        title=body.title,
        duration_seconds=body.duration_seconds,
        segments=[s.model_dump() for s in body.segments],
        user_id=current_user.user_id,
    )
    return RealtimeSaveResponse(
        transcript_id=str(result.transcript_id),
        segment_count=result.segment_count,
    )
