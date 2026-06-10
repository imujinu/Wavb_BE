# 실시간 녹음 저장 흐름을 파일 업로드 처리 모델로 통합

## Summary

기존 `server/plan/summary/realtime_summary.md`는 실시간 녹음 파일 URI 저장에 초점이 있었지만, 현재 백엔드의 기준 흐름은 `/files/upload -> /files/{id}/content -> /files/{id}/index -> /files/{id}/summary`다. 실시간 녹음도 이 흐름과 같은 `transcripts`, `segments`, `chunks`, `lecture_summaries` 데이터 구조를 만들도록 통합한다. 목표는 업로드 파일과 실시간 녹음이 같은 요약 생성 기반 데이터를 사용하게 하는 것이다.

## Key Changes

- 실시간 녹음 종료 시 프론트는 WAV 파일을 생성하고, `multipart/form-data`로 `file`, `title`, `duration_seconds`, `segments`를 전송한다.
- 백엔드는 `POST /audio/transcripts/realtime`를 특수 저장 API가 아니라 실시간 녹음용 업로드 어댑터로 재정의한다.
- 실시간 저장 API는 `UploadStorageService.save_upload()`로 원본 녹음 파일을 `/uploads/{user_id}/...`에 저장하고, WebSocket 세션에서 만든 `transcripts` row를 업로드 파일과 같은 형태로 갱신한다.
- 실시간에서 이미 받은 전사 `segments`는 OpenAI STT를 다시 돌리지 않고 공식 `segments`로 승격한다.
- 이후 처리는 기존 공통 파이프라인을 사용한다.
- 요약 생성은 기존 `/files/{transcript_id}/summary`의 `LectureSummaryService`를 그대로 사용한다.

## Public Interfaces

- `POST /audio/transcripts/realtime` 입력은 multipart다.
- 필드:
  - `transcript_id`: WebSocket 세션에서 받은 id
  - `file`: 녹음 WAV 파일
  - `title`: 저장 제목
  - `duration_seconds`: 녹음 길이
  - `segments`: JSON 문자열 배열, 서버 `temporary_segments`가 없을 때 fallback
- 응답은 기존 `transcript_id`, `segment_count`를 유지하고, `file_uri`, `status`, `content_status`, `index_status`를 추가한다.

## Backend Flow

1. WebSocket `ready`에서 만든 transcript row를 최종 row로 사용한다.
2. 종료 API는 같은 `transcript_id`를 받아 원본 파일을 저장한다.
3. 저장된 파일 URI, 원본 파일명, MIME type, duration, source type을 같은 row에 반영한다.
4. 서버 `temporary_segments`가 있으면 이를 우선 사용한다.
5. 서버 `temporary_segments`가 없으면 multipart `segments`를 temporary segment fallback으로 저장한다.
6. `TranscriptProcessingService.process()`를 호출해 기존 `/files/*` 모델과 같은 content/index 파이프라인을 실행한다.

## Test Plan

- 실시간 저장 API가 업로드 파일을 저장하고 같은 transcript row의 `source_audio_uri`를 `/uploads/...wav`로 채우는지 검증한다.
- 서버 `temporary_segments`가 있으면 클라이언트 segments를 다시 저장하지 않는지 검증한다.
- 서버 `temporary_segments`가 없으면 multipart `segments`를 fallback으로 저장하는지 검증한다.
- 처리 후 `content_status="completed"`, `index_status="completed"`가 응답되는지 검증한다.
- `/files/{transcript_id}/summary`가 chunk 기반 요약을 생성 가능한 상태인지 수동 검증한다.

## Assumptions

- 요약 생성의 기준 데이터는 `transcripts + segments + chunks`이며, 실시간 녹음도 이 구조를 따른다.
- 실시간 STT 결과를 신뢰하고, 저장 시 OpenAI STT를 다시 수행하지 않는다.
- 저장용 원본 파일은 WAV를 기본 포맷으로 사용한다.
- 구형 `/audio/transcripts` 업로드 API보다 `/files/*` 처리 모델을 통합 기준으로 삼는다.
