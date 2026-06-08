# 텍스트 변환과 인덱싱 분리 플랜

## Summary
- 현재 `/files/{transcript_id}/process`는 텍스트 추출/STT와 RAG 인덱싱을 한 번에 수행한다.
- 이를 명시적으로 `content` 단계와 `index` 단계로 분리한다.
- 기존 `/process`는 하위 호환용으로 남기되, 내부적으로 `content → index`를 순서대로 호출하는 orchestration API로 정리한다.
- 프론트는 기능 활성화 판단에 `status`만 쓰지 않고 `content_status`, `index_status`를 함께 사용한다.

## Key Changes
- 새 API 추가:
  - `POST /files/{transcript_id}/content`
    - 문서 텍스트 추출 또는 오디오 STT만 수행한다.
    - 성공 시 `content_status=completed`, `index_status`는 기존 값이 completed가 아니면 `pending` 유지.
    - 이미 `content_status=completed`이면 idempotent하게 현재 segment count를 반환한다.
  - `POST /files/{transcript_id}/index`
    - 이미 생성된 segments를 기반으로 chunks/search_chunks/embedding만 생성한다.
    - `content_status != completed`이면 `409 Conflict`와 `"Content is not completed."` 반환.
    - 성공 시 `index_status=completed`; `content_status=completed`이면 전체 `status=completed`.
  - 기존 `POST /files/{transcript_id}/process`
    - 호환 유지.
    - 내부에서 content가 미완료면 content 수행 후 index 수행.
    - content는 완료됐고 index만 실패/취소/미완료면 index만 재시도.

## Implementation Details
- `TranscriptProcessingService`를 단계별 public method로 분리한다:
  - `process_content(transcript_id, user_id) -> TranscriptProcessingResult`
  - `process_index(transcript_id, user_id) -> TranscriptProcessingResult`
  - `process(...)`는 위 두 메서드를 조합하는 wrapper로 변경.
- cancelled 상태 재시도 정책:
  - `content_status=completed`이고 `index_status != completed`인 transcript는 `/index` 또는 `/process`로 인덱싱 재시도 허용.
  - 재시도 시작 시 `cancel_requested_at`, `cancelled_at`, `error_message`를 초기화한다.
  - 이를 위해 repository에 `reset_processing_cancellation(transcript_id, user_id)` 또는 `restart_processing_stage(...)` 계열 메서드를 추가한다.
- 상태 업데이트 규칙:
  - content 시작: `status=processing`, `content_status=processing`, `index_status=pending`
  - content 성공: `content_status=completed`, `status=processing`
  - index 시작: `status=processing`, `index_status=processing`
  - index 성공: `status=completed`, `index_status=completed`
  - content 실패: `status=failed`, `content_status=failed`
  - index 실패: `status=failed`, `index_status=failed`, `content_status`는 유지
- 목록 응답 보강:
  - `UploadedFileResponse`와 `UploadedFileDetail`에 `content_status`, `index_status`, 가능하면 `error_message` 추가.
  - `list_transcripts_by_user()` SELECT에 해당 컬럼 포함.
- 기존 asyncpg 충돌 플랜과 정합성:
  - `ChunkMetadataService._enrich_chunk()` 내부의 DB 기반 cancellation check는 제거하고, `enrich_chunks()` 시작/끝 checkpoint에서만 취소 체크한다.
  - `_cancellation_checker()` lock 패치는 유지해도 되지만, 주 해결책은 병렬 task 내부 DB 취소 체크 제거로 둔다.

## Test Plan
- Route 테스트:
  - `/content`가 텍스트/segment만 만들고 chunk_count는 0으로 남기는지 확인.
  - `/index`가 content 미완료 상태에서 409를 반환하는지 확인.
  - `/index`가 content 완료 상태에서 chunks/search_chunks를 만들고 `index_status=completed`로 바꾸는지 확인.
  - 기존 `/process`가 content+index를 순서대로 완료하는지 확인.
- Retry 테스트:
  - `status=cancelled`, `content_status=completed`, `index_status=cancelled`, segments 존재, chunks 0 상태에서 `/index` 호출 시 인덱싱이 재시작되는지 확인.
  - 재시도 시 `cancel_requested_at/cancelled_at/error_message`가 초기화되는지 확인.
- Regression 테스트:
  - 이미 `index_status=completed`인 파일에 `/index`를 다시 호출하면 idempotent하게 count만 반환.
  - 병렬 chunk metadata 처리 중 `asyncpg InterfaceError: another operation is in progress`가 재발하지 않는지 fake checker로 검증.

## Assumptions
- 기존 DB 컬럼으로 충분하므로 신규 migration은 만들지 않는다.
- 기존 `/process`는 프론트 호환을 위해 제거하지 않는다.
- 프론트의 “완전 사용 가능” 조건은 `content_status === "completed" && index_status === "completed"`로 본다.
- 목록 화면에서도 단계 상태가 필요하므로 list API 응답에 `content_status/index_status`를 추가한다.
