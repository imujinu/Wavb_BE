# 구조화 요약 payload를 템플릿 레이아웃에 맞춰 한글 PDF 바이트로 렌더링하는 서비스.
#
# 기능 요약:
#   - LLM이 만든 섹션별 dict(payload)를 TemplateSpec.sections 순서대로 제목/본문으로 그린다.
#   - PDF 표준 코어 글꼴에는 한글 글리프가 없으므로 Noto Sans KR(유니코드 TTF/OTF)을 임베딩한다.
#
# 설계 이유:
#   - 렌더링을 LLM과 분리해 결정적으로 동작시키고, 동일 payload면 항상 같은 PDF가 나오도록 한다.

from pathlib import Path

from fastapi import HTTPException, status
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from services.pdf_templates import TemplateSpec
from settings import get_settings


# 번들 기본 글꼴 경로 — 서버 루트(services의 부모) 기준 assets/fonts 아래에 동봉한다.
_DEFAULT_FONT_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansKR-Regular.otf"
)
_FONT_FAMILY = "noto"


class SummaryPdfService:
    def __init__(self) -> None:
        settings = get_settings()
        # 설정에 글꼴 경로가 있으면 우선 사용하고, 없으면 번들 기본 글꼴을 사용한다.
        self._font_path = Path(settings.summary_pdf_font_path) if settings.summary_pdf_font_path else _DEFAULT_FONT_PATH

    # 구조화 요약 payload를 템플릿 양식으로 그려 PDF 바이트를 반환한다.
    # 동작 흐름:
    #   1. _new_pdf()로 한글 글꼴이 임베딩된 PDF 문서를 준비
    #   2. 머리말(제목/메타) 렌더
    #   3. 템플릿 섹션 순서대로 섹션 제목 + 본문 렌더
    #   4. PDF 바이트 반환
    # 파라미터:
    #   template: 렌더 기준 폼 명세(TemplateSpec)
    #   summary_payload: 섹션 key별 요약 값 (예: {"overview": "...", "decisions": ["..."]})
    #   header: 머리말 메타 (예: {"title": "주간 회의", "category": "meeting", "generated_at": "2026-05-31"})
    def render(
        self,
        template: TemplateSpec,
        summary_payload: dict,
        header: dict | None = None,
    ) -> bytes:
        header = header or {}

        # 1. 한글 글꼴이 임베딩된 PDF 문서 준비
        pdf = self._new_pdf()
        pdf.add_page()

        # 2. 머리말 렌더 — 폼 이름을 제목으로, 부가 메타를 작은 글씨로 표기
        self._render_header(pdf, template, header)

        # 3. 섹션 순서대로 제목 + 본문 렌더
        for section in template.sections:
            self._render_section(
                pdf,
                label=section.label,
                value=summary_payload.get(section.key, ""),
            )

        # 4. PDF 바이트 반환 (fpdf2는 bytearray를 반환하므로 bytes로 변환)
        return bytes(pdf.output())

    # 한글 글꼴을 임베딩한 FPDF 인스턴스를 생성한다.
    # 글꼴 파일이 없으면 한글이 모두 깨지므로 명확한 500으로 즉시 실패시킨다.
    def _new_pdf(self) -> FPDF:
        # 1. 글꼴 존재 검증 — 실 서비스에서 글꼴 누락은 배포 구성 오류이므로 500으로 알린다
        if not self._font_path.exists():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Summary PDF font is not available.",
            )

        # 2. 유니코드 글꼴 등록 후 본문 기본 글꼴로 설정
        pdf = FPDF()
        pdf.add_font(_FONT_FAMILY, "", str(self._font_path))
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.set_font(_FONT_FAMILY, size=11)
        return pdf

    # 문서 머리말(폼 제목 + 메타 정보)을 렌더한다.
    def _render_header(self, pdf: FPDF, template: TemplateSpec, header: dict) -> None:
        # 1. 폼 이름을 큰 글씨 제목으로
        pdf.set_font(_FONT_FAMILY, size=18)
        pdf.multi_cell(0, 10, template.name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # 2. 부가 메타(원문 제목/생성일)를 작은 글씨로 — 값이 있을 때만 출력
        meta_parts: list[str] = []
        if header.get("title"):
            meta_parts.append(f"원본: {header['title']}")
        if header.get("generated_at"):
            meta_parts.append(f"생성일: {header['generated_at']}")
        if meta_parts:
            pdf.set_font(_FONT_FAMILY, size=9)
            pdf.multi_cell(0, 6, "  |  ".join(meta_parts), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.ln(4)

    # 단일 섹션(제목 + 본문)을 렌더한다.
    # 본문 값은 문자열이면 그대로, 리스트면 불릿 항목으로 출력한다.
    def _render_section(self, pdf: FPDF, label: str, value) -> None:
        # 1. 섹션 제목
        pdf.set_font(_FONT_FAMILY, size=13)
        pdf.multi_cell(0, 8, label, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # 2. 섹션 본문 — 빈 값이면 안내 문구, 리스트면 불릿, 문자열이면 단락
        pdf.set_font(_FONT_FAMILY, size=11)
        body = self._format_body(value)
        pdf.multi_cell(0, 7, body, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)

    # 섹션 값을 PDF 본문 문자열로 변환한다 (리스트 → 불릿, 빈 값 → 안내 문구).
    def _format_body(self, value) -> str:
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            if not items:
                return "내용 없음"
            return "\n".join(f"• {item}" for item in items)

        text = str(value).strip()
        return text or "내용 없음"
