# 실시간 녹음 파일 URI 저장 개선 계획

## Summary
실시간 전사 녹음은 현재 `@siteed/audio-studio`를 스트리밍 전용으로 실행해서 녹음 파일이 생성되지 않고, 백엔드는 `source_audio_uri="realtime://recording"` placeholder만 저장한다. 녹음 종료 시 실제 WAV 파일을 생성하고 서버에 업로드한 뒤, 해당 실시간 transcript row의 `source_audio_uri`를 `/uploads/...` URI로 갱신한다.

## Key Changes
- 프론트 `useRealtimeTranscription`에서 `output.primary.enabled`를 `true`로 바꿔 WAV 파일을 생성하고, `stop()` 반환값에 `fileUri`, `filename`, `mimeType`, `durationMs`를 포함한다.
- 프론트 `saveRealtimeTranscript` API를 `multipart/form-data`로 확장해 JSON segments와 녹음 파일을 함께 보낸다.
- 백엔드 `POST /audio/transcripts/realtime`을 multipart 입력으로 변경한다:
  - `file`: 녹음 WAV 파일
  - `title`, `duration_seconds`, `segments`: 기존 실시간 저장 데이터
- 백엔드는 `UploadStorageService.save_upload()`로 파일을 `/uploads/{user_id}/...`에 저장하고, `TranscriptIngestionService.ingest_realtime_segments()`에 `source_uri`, `original_filename`, `mime_type`을 넘긴다.
- `ingest_realtime_segments()`는 더 이상 `realtime://recording`을 고정하지 않고 전달받은 서버 저장 URI를 `transcripts.source_audio_uri`에 저장한다.
- 기존 WebSocket 연결 시 임시 transcript row 생성은 유지하되, 최종 저장 API가 만드는 transcript와 중복 저장되는 현재 구조는 별도 정리 대상이다. 이번 변경은 앱 목록/상세에서 최종 저장된 row가 실제 파일 URI를 갖게 하는 데 집중한다.

## Public Interfaces
- `RealtimeSaveRequest` JSON body 방식은 multipart 방식으로 대체한다.
- `POST /audio/transcripts/realtime` 응답은 기존처럼 `transcript_id`, `segment_count`를 유지한다.
- 파일 목록/상세 응답의 `file_uri`는 기존 매핑 그대로 `transcripts.source_audio_uri`에서 나오며, 이제 `/uploads/...wav`가 반환된다.

## Test Plan
- 백엔드 unit test:
  - realtime 저장 API가 업로드 파일을 저장하고 `source_audio_uri`에 `/uploads/...` URI를 넘기는지 검증
  - 파일이 비어 있으면 400으로 실패하는지 검증
  - segments가 기존처럼 `segments`, `chunks`, search index 파이프라인으로 전달되는지 기존 테스트 유지
- 프론트 type/check:
  - `stop()` 결과에 녹음 파일 URI가 없으면 저장 실패 Alert를 띄우는지 확인
  - `saveRealtimeTranscript()`가 `FormData`에 file, title, duration_seconds, segments를 넣는지 확인
- 수동 검증:
  - 실시간 녹음 시작 → 전사 표시 → 저장
  - DB `transcripts.source_audio_uri`가 `realtime://recording`이 아니라 `/uploads/{user_id}/{uuid}.wav`인지 확인
  - 서버 upload 디렉터리에 실제 `.wav` 파일이 생성됐는지 확인
  - 앱 작업 목록/상세에서 `file_uri`가 내려오는지 확인

## Assumptions
- 사용자가 선택한 기본 방향은 “파일도 업로드”다.
- 녹음 파일 포맷은 `@siteed/audio-studio` primary output의 WAV를 사용한다.
- 실시간 STT는 지금처럼 WebSocket PCM 스트리밍을 계속 사용하고, 저장용 파일은 별도로 보관한다.
