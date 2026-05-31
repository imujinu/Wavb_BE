from pathlib import Path

import pytest
from fastapi import HTTPException

from services.pdf_templates import get_template
from services.summary_pdf_service import SummaryPdfService


# 번들 글꼴 경로 — 테스트는 실제 동봉 글꼴로 한글 렌더를 검증한다.
FONT_PATH = Path("assets/fonts/NotoSansKR-Regular.otf")


def make_service(font_path: Path) -> SummaryPdfService:
    service = SummaryPdfService.__new__(SummaryPdfService)
    service._font_path = font_path
    return service


def test_render_returns_pdf_bytes() -> None:
    service = make_service(FONT_PATH)
    template = get_template("meeting_weekly")
    payload = {
        "overview": "주간 회의 개요입니다.",
        "agenda": ["출시 일정", "테스트 계획"],
        "discussion": "일정 논의",
        "decisions": ["6월 출시 확정"],
        "action_items": ["QA 담당자 배정"],
    }

    pdf_bytes = service.render(
        template,
        payload,
        header={"title": "주간 회의", "generated_at": "2026-05-31"},
    )

    # %PDF 매직 바이트로 시작하는 비어있지 않은 PDF 바이트 검증
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes[:5] == b"%PDF-"
    assert len(pdf_bytes) > 1000


def test_render_handles_empty_sections() -> None:
    service = make_service(FONT_PATH)
    template = get_template("lecture_general")
    # 모든 섹션이 비어도 렌더가 실패하지 않아야 한다
    payload = {"topic": "", "key_points": [], "concepts": "", "keywords": []}

    pdf_bytes = service.render(template, payload)

    assert pdf_bytes[:5] == b"%PDF-"


def test_render_raises_when_font_missing() -> None:
    # 존재하지 않는 글꼴 경로 → 500
    service = make_service(Path("assets/fonts/does-not-exist.otf"))
    template = get_template("meeting_weekly")

    with pytest.raises(HTTPException) as exc:
        service.render(template, {"overview": "내용"})

    assert exc.value.status_code == 500
