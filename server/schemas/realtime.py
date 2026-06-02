from pydantic import BaseModel


class RealtimeSegmentInput(BaseModel):
    """저장 요청 시 클라이언트가 보내는 개별 세그먼트."""
    segment_index: int
    start_seconds: float
    end_seconds: float
    text: str


class RealtimeSaveRequest(BaseModel):
    """
    녹음 종료 후 전체 세그먼트를 DB에 저장하는 요청.

    WebSocket이 아닌 HTTP POST를 사용하는 이유:
    - WebSocket 세션 중에는 클라이언트가 임시 전사 결과를 로컬에 누적합니다.
    - 녹음 완료 후 한 번에 저장해 부분 저장/롤백 복잡도를 없앱니다.
    """
    domain_type: str       # "general", "legal", "medical", "science", "it", "religion"
    title: str
    duration_seconds: float
    segments: list[RealtimeSegmentInput]


class RealtimeSaveResponse(BaseModel):
    transcript_id: str
    segment_count: int
