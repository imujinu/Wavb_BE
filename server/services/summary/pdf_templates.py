# 요약 PDF 양식(템플릿) 정의를 코드 레지스트리로 관리하는 모듈.
#
# 기능 요약:
#   - 회의록/강의 요약 등 PDF 폼을 TemplateSpec(Pydantic) 으로 정의하고 dict 레지스트리에 등록한다.
#   - LLM은 각 섹션의 description 지시를 근거로 section.key 별 JSON을 생성하고,
#     PDF 렌더러는 section.label/순서를 그대로 사용해 결정적으로 그린다.
#
# 설계 이유:
#   - 템플릿 종류가 소수이고 배포 단위로 버전 관리되는 것이 자연스러우므로 DB 대신 코드로 둔다.
#   - TemplateSpec 을 Pydantic 모델로 정의해 .model_dump() 직렬화를 보장하므로,
#     추후 사용자 커스텀 폼이 필요하면 동일 스키마로 DB(templates 테이블)에 그대로 승격할 수 있다.

from pydantic import BaseModel, ConfigDict


# PDF 한 섹션의 명세.
# key: LLM이 생성할 JSON payload의 키 / 렌더 시 payload에서 값을 꺼낼 키
# label: PDF에 출력될 섹션 제목
# description: 해당 섹션을 어떻게 작성할지 LLM에게 전달하는 지시문
class SectionSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    description: str


# 하나의 PDF 폼(양식) 명세.
# id: 템플릿 식별자 (요청 body의 template_id, summary_documents.template_id 로 저장)
# name: 사람이 읽는 폼 이름 (앱 목록 표시용)
# category: 폼 분류 ("meeting" | "lecture")
# sections: 렌더 순서를 가지는 섹션 목록
class TemplateSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    category: str
    sections: list[SectionSpec]


# 등록된 모든 템플릿. 키는 template_id 와 동일하게 유지한다.
TEMPLATE_REGISTRY: dict[str, TemplateSpec] = {
    "meeting_weekly": TemplateSpec(
        id="meeting_weekly",
        name="주간 팀 회의록",
        category="meeting",
        sections=[
            SectionSpec(
                key="overview",
                label="회의 개요",
                description="회의 목적과 전반적인 맥락을 2~3문장으로 요약한다.",
            ),
            SectionSpec(
                key="agenda",
                label="주요 안건",
                description="다뤄진 안건을 항목별로 정리한다. 근거가 없으면 비운다.",
            ),
            SectionSpec(
                key="discussion",
                label="논의 내용",
                description="안건별 핵심 논의 내용을 요약한다.",
            ),
            SectionSpec(
                key="decisions",
                label="결정 사항",
                description="확정된 결정만 항목으로 정리한다.",
            ),
            SectionSpec(
                key="action_items",
                label="액션 아이템",
                description="담당자와 기한이 드러난 후속 할 일만 정리한다.",
            ),
        ],
    ),
    "meeting_client": TemplateSpec(
        id="meeting_client",
        name="고객사 미팅록",
        category="meeting",
        sections=[
            SectionSpec(
                key="overview",
                label="미팅 개요",
                description="고객사/일시/목적 등 미팅 맥락을 2~3문장으로 요약한다.",
            ),
            SectionSpec(
                key="client_requests",
                label="고객 요구사항",
                description="고객이 요청하거나 강조한 사항을 항목별로 정리한다.",
            ),
            SectionSpec(
                key="discussion",
                label="논의 내용",
                description="요구사항에 대한 논의와 답변을 요약한다.",
            ),
            SectionSpec(
                key="decisions",
                label="합의/결정 사항",
                description="양측이 합의하거나 확정한 사항만 정리한다.",
            ),
            SectionSpec(
                key="next_steps",
                label="다음 단계",
                description="후속 일정과 담당이 드러난 다음 단계를 정리한다.",
            ),
        ],
    ),
    "meeting_decision": TemplateSpec(
        id="meeting_decision",
        name="의사결정 회의록",
        category="meeting",
        sections=[
            SectionSpec(
                key="overview",
                label="안건 개요",
                description="결정이 필요한 안건과 배경을 2~3문장으로 요약한다.",
            ),
            SectionSpec(
                key="options",
                label="검토 대안",
                description="논의된 대안과 각 대안의 장단점을 정리한다.",
            ),
            SectionSpec(
                key="decisions",
                label="결정 사항",
                description="최종 결정과 그 사유를 명확히 정리한다.",
            ),
            SectionSpec(
                key="risks",
                label="리스크/고려사항",
                description="결정에 따른 리스크나 추가 고려사항을 정리한다.",
            ),
            SectionSpec(
                key="action_items",
                label="액션 아이템",
                description="담당자와 기한이 드러난 후속 할 일만 정리한다.",
            ),
        ],
    ),
    "lecture_general": TemplateSpec(
        id="lecture_general",
        name="일반 강의 요약",
        category="lecture",
        sections=[
            SectionSpec(
                key="topic",
                label="강의 주제",
                description="강의의 핵심 주제를 1~2문장으로 요약한다.",
            ),
            SectionSpec(
                key="key_points",
                label="요점 정리",
                description="강의에서 다룬 핵심 요점을 항목별로 정리한다.",
            ),
            SectionSpec(
                key="concepts",
                label="핵심 개념",
                description="기억해야 할 핵심 개념과 설명을 정리한다.",
            ),
            SectionSpec(
                key="keywords",
                label="키워드",
                description="강의를 대표하는 키워드를 정리한다.",
            ),
        ],
    ),
    "lecture_cs": TemplateSpec(
        id="lecture_cs",
        name="전공/기술 강의 요약",
        category="lecture",
        sections=[
            SectionSpec(
                key="topic",
                label="강의 주제",
                description="다룬 기술/전공 주제를 1~2문장으로 요약한다.",
            ),
            SectionSpec(
                key="concepts",
                label="핵심 개념/이론",
                description="핵심 개념·이론·정의를 항목별로 정리한다.",
            ),
            SectionSpec(
                key="examples",
                label="예시/적용",
                description="강의에서 제시된 예시나 적용 사례를 정리한다.",
            ),
            SectionSpec(
                key="key_points",
                label="요점 정리",
                description="시험/실무에서 기억해야 할 요점을 정리한다.",
            ),
            SectionSpec(
                key="keywords",
                label="키워드",
                description="강의를 대표하는 기술 키워드를 정리한다.",
            ),
        ],
    ),
}


# template_id 로 단일 템플릿 명세를 조회한다.
# 필요성: 라우트/서비스가 template_id 문자열만으로 폼 정의에 접근하도록 진입점을 단일화한다.
#         추후 DB 전환 시 이 함수 내부 구현만 교체하면 호출부는 변경되지 않는다.
# 파라미터:
#   template_id: 조회할 템플릿 식별자 (예: "meeting_weekly")
# 반환: 존재하면 TemplateSpec, 없으면 None
def get_template(template_id: str) -> TemplateSpec | None:
    return TEMPLATE_REGISTRY.get(template_id)


# 등록된 전체 템플릿 목록을 반환한다.
# 필요성: GET /audio/summary-templates 가 앱에 폼 목록을 내려주기 위해 사용한다.
def list_templates() -> list[TemplateSpec]:
    return list(TEMPLATE_REGISTRY.values())
