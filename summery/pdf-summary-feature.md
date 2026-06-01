# 구현 요약: 스크립트 → 템플릿 기반 요약 PDF 생성

## 개요
저장된 STT 스크립트(`transcripts.full_text`)를 선택한 PDF 양식(회의록/강의 요약 등)으로 LLM 요약하여
한글 PDF로 생성·다운로드하고, 생성 결과(구조화 payload)를 저장해 **수정 후 재생성**까지 지원하는 기능을 추가했다.

- 브랜치: `feat/pdf`
- 처리 시간: **약 11분 32초** (2026-05-31 10:02:58 → 10:14:30)

## 동작 흐름
```
[템플릿 조회] GET /audio/summary-templates
  → 등록된 폼 목록(TemplateSpec) 반환

[생성] POST /audio/transcripts/{transcript_id}/summary-pdf   (Bearer)
  body: { "template_id": "meeting_weekly" }
    → 템플릿 검증(없으면 404)
    → 소유 스크립트 조회(없음/비소유 404, full_text 빈값 409)
    → TemplatedSummaryService: 섹션 스키마에 맞춘 구조화 JSON 생성(json_object)
    → insert_summary_document: payload 영속화
    → SummaryPdfService: Noto Sans KR 임베딩 한글 PDF 렌더
    → application/pdf 다운로드 + X-Summary-Document-Id 헤더

[수정→재생성] PUT /audio/summary-documents/{document_id}   (Bearer)
  body: { "payload": { ...수정된 섹션... } }
    → 소유 문서 조회(없음/비소유 404)
    → update_summary_document_payload: 수정 payload 저장
    → 저장된 template_id로 양식 복원 → PDF 재렌더(LLM 재호출 없음)
    → application/pdf 다운로드
```

핵심 설계: **LLM은 섹션별 구조화 JSON만 생성, PDF 레이아웃은 결정적 코드가 렌더**.
→ 출력이 안정적이고, 잘못된 섹션만 골라 수정→재렌더가 가능하다.

## 신규/수정 파일
| 파일 | 종류 | 내용 |
|---|---|---|
| `services/pdf_templates.py` | 신규 | `TemplateSpec`/`SectionSpec`(Pydantic) + `TEMPLATE_REGISTRY`(5종) + `get_template`/`list_templates` |
| `services/templated_summary_service.py` | 신규 | 템플릿 섹션 스키마에 맞춘 구조화 요약(긴 입력 잘라내기, 누락 섹션 보정) |
| `services/summary_pdf_service.py` | 신규 | `fpdf2` + Noto Sans KR 임베딩으로 한글 PDF 렌더 |
| `db/migrations/005_add_summary_documents.sql` | 신규 | `summary_documents` 테이블(payload JSONB) + 인덱스 |
| `assets/fonts/NotoSansKR-Regular.otf` | 신규 | 번들 한글 글꼴(OFL) |
| `schemas/rag.py` | 수정 | `TranscriptDetail`, `SummaryDocumentCreate`, `SummaryDocumentDetail` 추가 |
| `repositories/rag_repository.py` | 수정 | `get_transcript_by_id`, `insert/get/update_summary_document` 추가 |
| `routes/audio.py` | 수정 | 엔드포인트 3개 + DI 2개 추가(기존 경로 불변) |
| `settings.py` | 수정 | `SUMMARY_PDF_MODEL/MAX_INPUT_CHARS/FONT_PATH` 추가 |
| `pyproject.toml` / `uv.lock` | 수정 | `fpdf2>=2.7` 의존성 추가 |
| `.env.example` | 수정 | 신규 설정 문서화 |
| `tests/test_templated_summary_service.py` | 신규 | 섹션 매핑/누락 보정/빈 입력 422/긴 입력 잘라내기/잘못된 JSON 502 |
| `tests/test_summary_pdf_service.py` | 신규 | PDF 매직바이트/빈 섹션/글꼴 미존재 500 |
| `tests/test_summary_pdf_routes.py` | 신규 | 템플릿 목록/생성(404·409·정상)/수정(404·정상) |
| `tests/test_rag_persistence.py` | 수정 | `get_transcript_by_id`·summary_document 영속화 테스트 보강 |

## 제공 템플릿(폼)
- `meeting_weekly` 주간 팀 회의록
- `meeting_client` 고객사 미팅록
- `meeting_decision` 의사결정 회의록
- `lecture_general` 일반 강의 요약
- `lecture_cs` 전공/기술 강의 요약

## 테스트 결과
- 신규 테스트 **20개 전부 통과** (요약 서비스 5 + PDF 렌더 3 + 라우트 7 + 영속화 5)
- 전체 스위트: **101 passed, 5 failed**
- 실패 5건은 **모두 이번 작업과 무관한 사전 실패**이며, `feat/pdf` 커밋 시점(stash)에서도 동일하게 실패함을 확인:
  - `test_search_chunks_hybrid_*` 3건 — 기존 RRF 구현 vs 가중합 기대값 불일치(기존 테스트 미갱신)
  - `test_auth_service` 2건 — PyJWT의 HMAC 키 길이 검증(테스트용 짧은 키), 환경 이슈

## 검증 방법
```bash
# server/ 디렉토리 기준
uv sync
uv run pytest tests/test_templated_summary_service.py tests/test_summary_pdf_service.py tests/test_summary_pdf_routes.py
uv run pytest tests/test_rag_persistence.py -k "transcript_by_id or summary_document"

# DB 마이그레이션(005) 적용 후 E2E
docker-compose up -d
uv run uvicorn main:app --reload
# GET /audio/summary-templates → 폼 목록
# POST /audio/transcripts/{id}/summary-pdf {"template_id":"meeting_weekly"} → application/pdf + X-Summary-Document-Id
# PUT  /audio/summary-documents/{document_id} {"payload":{...}} → 수정 반영 PDF
```

## 비고 / 후속 과제
- `db/migrations/005_*.sql`는 멱등(`CREATE TABLE IF NOT EXISTS`)이나, 운영 DB 적용은 별도 수동 실행이 필요(프로젝트에 자동 마이그레이션 러너 없음).
- 번들 글꼴은 OFL 라이선스 Noto Sans KR. git LFS 전환 여부는 팀 정책에 따라 결정.
- 사용자 커스텀 폼이 필요해지면 `TemplateSpec`(Pydantic)을 그대로 `templates` 테이블에 승격하고 `get_template`/`list_templates` 내부만 교체하면 됨(엔드포인트 시그니처 유지).
