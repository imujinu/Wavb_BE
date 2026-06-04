from typing import Literal

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
    title: str
    duration_seconds: float
    segments: list[RealtimeSegmentInput]


class RealtimeSaveResponse(BaseModel):
    transcript_id: str
    segment_count: int


class RealtimeSummaryEvent(BaseModel):
    """
    WebSocket을 통해 실시간으로 전송되는 요약 이벤트.

    프론트엔드는 type 필드로 메시지 타입을 분기하여 처리합니다.
    """
    type: Literal["summary"] = "summary"
    summary: str        # GPT 요약문
    full_text: str      # 원문 전체 (프론트 "전체 보기" 버튼용)
    segment_index: int  # 몇 번째 구간인지 (0부터, 프론트 렌더링 매칭용)
    # 이 요약이 덮는 transcript(final) 범위 — FE가 해당 범위의 실시간 라인만 정확히 collapse한다.
    # 누적된 final이 없었던 빈 구간이면 둘 다 -1.
    start_final_index: int = -1  # 첫 final_index (포함)
    end_final_index: int = -1    # 마지막 final_index (포함)
    keywords: list[str] = []     # 요약문 기반 핵심 키워드 (3~6개)
