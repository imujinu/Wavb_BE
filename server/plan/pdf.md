네, 수정된 전체 플랜입니다.

---

# 계획: 스크립트 → 템플릿 기반 요약 PDF 생성 (회의록 / 강의 요약)

## Context (왜 이 작업을 하는가)

현재 Recordoc 백엔드는 오디오 → STT(Whisper) → 스크립트(`transcripts.full_text`) 저장까지 수행하지만, 저장된 스크립트를 **사람이 읽는 산출물(회의록/강의 요약 PDF)** 로 만드는 경로가 없다. 사용자는 다음 흐름을 원한다:

1. **템플릿 목록 조회** — 사용 가능한 폼 목록 확인
2. **스크립트 선택** — 저장된 transcript 중 하나 (`transcript_id`)
3. **템플릿 선택** — 원하는 폼 선택 (`template_id`)
4. **요약본 생성** — 선택 스크립트를 선택 템플릿 구조에 맞게 LLM 요약 → PDF 다운로드

추가로 **"PDF 내용이 잘못되면 수정해서 다시 만들 수 있어야 한다"** 는 요구가 있다. 따라서 생성된 **구조화 요약 결과(JSON payload)를 DB에 영속화**하고, 그 payload를 **수정 후 PDF만 재렌더**하는 경로를 1차 범위에 포함한다.

### 설계 핵심 결정

- **역할 분리**: LLM은 템플릿 섹션 스키마에 맞는 **구조화 JSON만** 생성하고, PDF 렌더링은 **결정적 코드**가 담당.
- **템플릿은 코드 레지스트리(dict)로 관리**: 현재는 코드에서 관리하되, 나중에 사용자 커스텀 폼 기능 추가 시 DB로 자연스럽게 마이그레이션할 수 있도록 `TemplateSpec`을 **Pydantic 모델**로 정의해 JSON 직렬화를 보장한다.
- **영속화 필수**: 수정→재생성 요구 때문에 `summary_documents` 테이블에 구조화 payload를 저장한다.
- **템플릿 목록 조회 엔드포인트 제공**: 앱이 폼 목록을 동적으로 불러올 수 있도록 `GET /audio/summary-templates` 추가. 나중에 DB로 전환해도 이 엔드포인트 시그니처는 그대로 유지된다.

---

## 구현 단계

### Step 1: transcript 읽기 경로 추가
- `server/schemas/rag.py`: 읽기 전용 모델 `TranscriptDetail`(id, user_id, domain_type, title, full_text, summary, duration_seconds, language, status, created_at) 추가.
- `server/repositories/rag_repository.py`: `get_transcript_by_id(transcript_id, user_id=None) -> TranscriptDetail | None` 추가. `WHERE id=$1` + (user_id 주어지면) `AND user_id=$2` 소유권 필터.
- **격리**: 신규 메서드/모델 추가만. 기존 경로 무영향.

### Step 2: PDF 템플릿 정의(코드 레지스트리) + 설정값
- 신규 `server/services/pdf_templates.py`:
  - `TemplateSpec`을 **Pydantic 모델**로 정의:
    ```python
    class TemplateSpec(BaseModel):
        id: str
        name: str                     # 사람이 읽는 폼 이름 (예: "주간 팀 회의록")
        category: str                 # "meeting" | "lecture" 등 카테고리
        sections: list[SectionSpec]   # 섹션 순서/라벨/LLM 지시
    
    class SectionSpec(BaseModel):
        key: str        # JSON payload key
        label: str      # PDF에 표시될 섹션 제목
        description: str  # LLM에게 전달할 섹션 작성 지시
    ```
  - `TEMPLATE_REGISTRY: dict[str, TemplateSpec]`로 관리. 예시 폼:
    - `"meeting_weekly"` — 주간 팀 회의록
    - `"meeting_client"` — 고객사 미팅록
    - `"meeting_decision"` — 의사결정 회의록
    - `"lecture_general"` — 일반 강의 요약
    - `"lecture_cs"` — 전공/기술 강의 요약
  - `get_template(template_id: str) -> TemplateSpec | None` 헬퍼 함수 제공.
- `server/settings.py` 추가:
  - `summary_pdf_model` (기본 `gpt-4o-mini`)
  - `summary_pdf_max_input_chars` (기본 `48000`)
  - `summary_pdf_font_path` (기본 `""`)
- **격리**: 신규 파일 + 기본값 설정 필드만 추가.

### Step 3: 구조화 요약 서비스 `TemplatedSummaryService`
- 신규 `server/services/templated_summary_service.py`:
  - 생성자: `AsyncOpenAI` 키 검증, `self._model = settings.summary_pdf_model`.
  - `async def summarize_for_template(transcript_text, template: TemplateSpec, title, domain_type) -> dict`:
    1. 빈 텍스트 → 422.
    2. `len <= summary_pdf_max_input_chars` → 단일 호출, 초과 → 앞에서 자르기(1차 단순 처리).
    3. `response_format={"type":"json_object"}` + temperature 0.2로 `TemplateSpec.sections`의 스키마에 맞는 JSON 생성. 각 섹션의 `description`을 프롬프트에 포함해 LLM이 섹션별 의도를 정확히 파악하도록 한다.
    4. 누락 섹션 보정(빈 문자열로 채움).
  - 에러 매핑: `APIError→502`, 빈 응답→502, 기타→500.
- **격리**: 신규 서비스. 기존 서비스 수정 없음.

### Step 4: PDF 렌더링 서비스 `SummaryPdfService` + 의존성
- `server/pyproject.toml`: `fpdf2>=2.7` 추가.
- 한글 글꼴 동봉: `server/assets/fonts/NotoSansKR-Regular.ttf` (OFL). git LFS 또는 빌드 시 다운로드 방식은 팀 내 결정 필요.
- 신규 `server/services/summary_pdf_service.py`:
  - `def render(template: TemplateSpec, summary_payload: dict, header: dict) -> bytes`: `TemplateSpec.sections` 순서로 제목/메타(생성일·title·category)/섹션 렌더. `add_font(..., uni=True)`로 한글 임베딩. 글꼴 미존재 시 500.
- **격리**: 신규 의존성/파일/에셋만.

### Step 5: 생성 이력 영속화 `summary_documents`
- `server/db/migrations/005_add_summary_documents.sql`: `summary_documents`(id UUID PK, transcript_id FK CASCADE, user_id, template_id TEXT, payload JSONB, model TEXT, created_at, updated_at). 인덱스 `(transcript_id)`, `(user_id, created_at DESC)`. `CREATE TABLE IF NOT EXISTS`로 멱등.
- `server/schemas/rag.py`: `SummaryDocumentCreate` / `SummaryDocumentDetail` 모델 추가.
- `server/repositories/rag_repository.py`:
  - `insert_summary_document(...) -> UUID`
  - `get_summary_document_by_id(document_id, user_id=None) -> SummaryDocumentDetail | None`
  - `update_summary_document_payload(document_id, payload, user_id=None) -> bool`
- **격리**: 신규 테이블/메서드/모델만.

### Step 6: 라우트 결선
`server/routes/audio.py`에 엔드포인트 3개 추가.

- **템플릿 목록 조회** `GET /audio/summary-templates`
  - 인증 불필요(또는 선택).
  - `TEMPLATE_REGISTRY`의 전체 `TemplateSpec` 목록 반환. 나중에 DB로 전환해도 이 시그니처 유지.
  - 응답 예시:
    ```json
    [
      { "id": "meeting_weekly", "name": "주간 팀 회의록", "category": "meeting", "sections": [...] },
      ...
    ]
    ```

- **생성** `POST /audio/transcripts/{transcript_id}/summary-pdf`
  - body: `{ "template_id": "meeting_weekly" }`
  - 흐름: 인증 → `get_template(template_id)`(없음→404) → `get_transcript_by_id(id, user_id)`(없음/비소유→404, `full_text` 빈값→409) → `TemplatedSummaryService.summarize_for_template(...)` → `insert_summary_document(payload)` → `SummaryPdfService.render(...)` → `StreamingResponse(application/pdf)` + `X-Summary-Document-Id` 헤더.

- **수정 후 재렌더** `PUT /audio/summary-documents/{document_id}`
  - body: `{ "payload": { ...수정된 섹션... } }`
  - 흐름: 인증 → `get_summary_document_by_id(id, user_id)`(없음/비소유→404) → `update_summary_document_payload(...)` → 저장된 `template_id`로 `get_template(...)` → `SummaryPdfService.render(...)` → `StreamingResponse(application/pdf)`. LLM 재호출 없음.

- **격리**: 기존 경로/시그니처 불변.

### Step 7: 테스트
- `tests/test_templated_summary_service.py`: 섹션 스키마 매핑, 누락 섹션 보정, 빈 transcript 422, 긴 입력 잘라내기.
- `tests/test_summary_pdf_service.py`: `render()`가 `%PDF` 매직 바이트로 시작하는 bytes 반환, 섹션 라벨 포함, 글꼴 미존재 시 500.
- `tests/test_audio_routes.py`: 생성 라우트 — 템플릿 없음→404, transcript 없음/비소유→404, `full_text` 빈값→409, 정상→`application/pdf` + `X-Summary-Document-Id`. 수정 라우트 — 문서 없음/비소유→404, 정상→`application/pdf`. 템플릿 목록 조회 — 정상→200 + 목록.
- `tests/test_rag_persistence.py` 보강: `get_transcript_by_id` 소유권 필터, `insert/get/update_summary_document`.

---

## 완료 후 서비스 흐름

```
[템플릿 조회] GET /audio/summary-templates
  → TEMPLATE_REGISTRY 목록 반환 (앱이 폼 목록 동적 렌더링)

[생성] POST /audio/transcripts/{transcript_id}/summary-pdf
  body: { "template_id": "meeting_weekly" }
    → get_template(template_id)                               # 없으면 404
    → get_transcript_by_id(id, user_id)                       # 없음/비소유 404, full_text 빈값 409
    → TemplatedSummaryService.summarize_for_template(...)     # TemplateSpec 기반 JSON 생성
    → insert_summary_document(template_id, payload)           # 수정용 영속화
    → SummaryPdfService.render(template, payload, header)     # 결정적 렌더링
    → StreamingResponse(application/pdf) + X-Summary-Document-Id

[수정→재생성] PUT /audio/summary-documents/{document_id}
  body: { "payload": { ...수정된 섹션... } }
    → get_summary_document_by_id(id, user_id)                 # 없음/비소유 404
    → update_summary_document_payload(...)
    → get_template(저장된 template_id)
    → SummaryPdfService.render(template, payload)             # LLM 재호출 없음
    → StreamingResponse(application/pdf)
```

---

## 신규/수정 파일 요약

| 파일 | 종류 | 단계 |
|---|---|---|
| `server/schemas/rag.py` | 수정(모델 추가) | 1, 5 |
| `server/repositories/rag_repository.py` | 수정(메서드 추가) | 1, 5 |
| `server/services/pdf_templates.py` | 신규 | 2 |
| `server/settings.py` | 수정(필드 추가) | 2 |
| `server/services/templated_summary_service.py` | 신규 | 3 |
| `server/pyproject.toml` | 수정(fpdf2 추가) | 4 |
| `server/assets/fonts/NotoSansKR-Regular.ttf` | 신규 에셋 | 4 |
| `server/services/summary_pdf_service.py` | 신규 | 4 |
| `server/db/migrations/005_add_summary_documents.sql` | 신규 | 5 |
| `server/routes/audio.py` | 수정(엔드포인트 3개 + DI 추가) | 6 |
| `server/tests/test_*` | 신규/보강 | 7 |

---

## 나중에 커스텀 폼 기능 추가 시 마이그레이션 경로

`TemplateSpec`이 Pydantic 모델이므로 `.model_dump()`로 바로 JSONB에 저장 가능. 마이그레이션 시 변경 범위는 다음으로 한정된다:

1. `templates` 테이블 신규 생성 + 기존 레지스트리 데이터 삽입
2. `get_template()` 헬퍼를 코드 dict 조회 → DB 조회로 교체
3. `GET /audio/summary-templates` 내부 구현만 교체 (시그니처 유지)

앱/서비스 레이어 코드는 수정 없음.