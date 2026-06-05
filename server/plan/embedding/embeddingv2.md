# 파일 저장과 비용 발생 처리를 분리하는 구조 개선 플랜

## Summary

- 파일 업로드는 원본 파일 저장과 transcript 메타데이터 생성만 수행한다.
- 문서 텍스트 추출, 음성 STT, chunk 생성, embedding/search index 저장은 사용자가 명시적으로 처리 요청을 보낼 때 실행한다.
- 녹음 중 실시간 STT 결과는 프론트가 마지막에 다시 보내지 않는다. 서버가 `is_final=true` 이벤트를 받을 때마다 임시 segment로 저장한다.
- 프론트 파일 목록에는 대표 `status`만 내려주고, 내부 DB는 `content_status`, `index_status`로 단계 상태를 관리한다.

## Key Changes

- DB
  - `transcripts`에 내부 처리 상태와 원본 타입을 추가한다.
    - `source_type`: `audio | pdf | ppt`
    - `content_status`: `pending | processing | completed | failed`
    - `index_status`: `pending | processing | completed | failed`
    - `processed_at`, `indexed_at`
  - `temporary_segments` 전용 테이블을 추가한다.
    - `id`, `transcript_id`, `segment_index`, `start_seconds`, `end_seconds`, `text`, `raw_metadata`, `created_at`
    - WebSocket 스트리밍 중 STT provider가 `is_final=true`로 확정한 이벤트만 저장한다.
  - `transcripts.temporary_text`는 저장된 temporary segment들의 전체 미리보기/목록 표시용으로 유지할 수 있다.
  - 기존 `transcripts.status`는 프론트 목록용 대표 상태로 유지한다.
    - 업로드/녹음 저장 직후: `uploaded`
    - 사용자 처리 중: `processing`
    - 텍스트화+인덱싱 완료: `completed`
    - 처리 실패: `failed`

- 파일 업로드 API
  - `POST /files/upload`
  - 요청은 기존처럼 `file`, optional `file_name`.
  - 동작은 원본 저장 + transcript row 생성까지만 수행한다.
  - 문서 텍스트 추출, STT, chunk 생성, embedding 호출은 하지 않는다.
  - 응답과 `GET /files`, `GET /work-items` 목록에는 대표 `status`만 내려준다.

- 사용자 처리 API
  - `POST /files/{transcript_id}/process`
  - 인증 사용자 본인 파일만 처리한다.
  - 응답은 일반 JSON으로 반환한다. 실시간 처리 알림은 v1 범위에서 제외한다.
  - 문서 파일이면 저장된 원본 경로에서 텍스트를 추출하고, segment/chunk/search index를 생성한다.
  - 음성 파일이면 `temporary_segments`가 있으면 STT를 재호출하지 않고 공식 `segments`로 승격한다.
  - 음성 파일인데 `temporary_segments`가 없으면 저장된 원본 오디오로 STT를 실행한다.
  - `content_status=completed`이면 텍스트화는 재실행하지 않고 인덱싱 단계만 이어서 수행한다.
  - `index_status=completed`이면 추가 토큰 소비 없이 완료 응답을 반환한다.
  - 응답 예시:
    ```json
    {
      "transcript_id": "...",
      "status": "completed",
      "content_status": "completed",
      "index_status": "completed",
      "segment_count": 12,
      "chunk_count": 4
    }
    ```

- 녹음/실시간 STT 저장 구조
  - `WS /audio/realtime/connect`는 지금처럼 실시간 transcript 이벤트를 프론트에 내려준다.
  - WebSocket 시작 시 서버가 transcript row 또는 recording session row를 먼저 만든다.
  - STT provider가 보내는 `is_final=true` 이벤트는 서버가 즉시 `temporary_segments`에 append 저장한다.
  - `interim` 이벤트는 DB에 저장하지 않고 프론트 표시용으로만 내려준다.
  - 25~30초 summary buffer는 요약 이벤트 생성을 위한 묶음일 뿐, 저장 단위가 아니다.
  - 녹음 종료 시에는 이미 저장된 temporary segment들을 합쳐 `temporary_text`를 갱신하고, status는 `uploaded`로 유지한다.
  - 이후 사용자가 처리 요청을 보내면 temporary segment를 공식 segment로 승격하고 chunk/embedding을 진행한다.

- Service 구조
  - 기존 `FileIngestionService.ingest_upload()`는 저장 전용 업로드 서비스로 축소한다.
  - 새 `TranscriptProcessingService.process(transcript_id, user_id)`를 만든다.
  - `TranscriptIngestionService`는 저장된 원본 파일 또는 temporary segment에서 처리할 수 있도록 재구성한다.
  - `TranscriptionService`와 `DocumentTextExtractionService`는 `UploadFile`뿐 아니라 저장된 `Path` 기반 입력도 처리할 수 있게 확장한다.
  - `UploadStorageService`에 `/uploads/...` URI를 안전하게 로컬 `Path`로 되돌리는 resolver를 추가한다.

## Test Plan

- `POST /files/upload`
  - 업로드 시 원본 파일만 저장되고 transcript row가 `status=uploaded`, `content_status=pending`, `index_status=pending`으로 생성된다.
  - 문서 텍스트 추출 서비스, STT 서비스, embedding 서비스가 호출되지 않는다.
  - 목록 응답에는 대표 `status`만 포함된다.

- `POST /files/{transcript_id}/process`
  - PDF/PPT 처리 요청 시 텍스트 추출, segment 저장, chunk 저장, embedding/search_chunks 저장이 순서대로 실행된다.
  - 음성 처리 요청 시 temporary segment가 있으면 STT를 호출하지 않고 공식 segment로 승격한다.
  - temporary segment가 없으면 저장된 오디오 파일로 STT를 호출한다.
  - 이미 인덱싱 완료된 파일은 외부 API 호출 없이 완료 JSON을 반환한다.
  - 다른 사용자 파일은 `404`로 거부한다.
  - 텍스트화 실패 시 `content_status=failed`, 인덱싱 실패 시 `index_status=failed`로 저장하고 에러 JSON을 반환한다.

- Realtime recording
  - WebSocket 중 `interim` 이벤트는 DB에 저장하지 않는다.
  - `is_final=true` 이벤트는 서버가 temporary segment로 저장한다.
  - 녹음 종료 후 프론트가 segments를 다시 POST하지 않아도 서버에 임시 transcript가 남아 있다.
  - 이후 process API 호출 시 저장된 temporary segment가 우선 사용된다.

- Repository
  - transcript 생성 SQL에 `source_type`, `content_status`, `index_status`가 포함된다.
  - temporary segment insert는 `transcript_id`, `segment_index` 기준으로 중복 저장을 방지한다.
  - 처리 상태 업데이트 메서드는 `user_id` 소유권 조건을 포함한다.

## Assumptions

- 사용자 토큰을 소비하는 작업은 `POST /files/{transcript_id}/process` 호출 시점에만 발생한다.
- `content_status`는 문서 텍스트 추출과 음성 STT/임시 segment 승격을 모두 의미한다.
- `index_status`는 chunk 생성 이후 embedding/search index 저장 완료 여부를 의미한다.
- 프론트 목록은 우선 대표 `status`만 사용한다.
- 실시간 STT의 `final`은 녹음 종료 전체 결과가 아니라 provider가 스트리밍 중 확정한 개별 발화 segment에 가깝다.
- RAG 검색 대상은 `index_status=completed`인 transcript로 제한한다.
