# Realtime 백엔드 작업 요약

## 수정한 파일 및 변경 내용

**파일:** `server/services/realtime_transcription_service.py` (line 41)

| 구분 | 내용 |
|------|------|
| 변경 전 | `tempfile.NamedTemporaryFile(suffix=".webm", delete=False)` |
| 변경 후 | `tempfile.NamedTemporaryFile(suffix=".m4a", delete=False)` |

**변경 이유:**
expo-av `HIGH_QUALITY` 프리셋은 iOS/Android 모두 m4a(AAC) 컨테이너로 오디오를 출력한다.
OpenAI Whisper API는 파일 확장자를 기준으로 MIME 타입을 결정하므로, `.webm` suffix를 유지하면
m4a 바이트를 잘못된 포맷으로 처리할 수 있다. `.m4a`로 변경하면 Whisper API가 올바른 codec
(AAC)으로 디코딩한다. Whisper는 m4a를 공식 지원한다.

---

## OAuth 충돌 여부 확인 결과

**충돌 없음. 작업 안전.**

확인 범위:
- `server/routes/` 전체 파일 목록 및 `routes/auth.py` 전체 내용 검토
- `server/services/` 전체 파일 목록 및 `services/auth_service.py` 전체 내용 검토
- `server/repositories/` 파일 목록 검토
- 프로젝트 전체에서 `TODO`, `FIXME`, `oauth`, `google`, `kakao`, `naver`, `social` 키워드 grep 실행

**결과:**
- OAuth 관련 미완성 파일, TODO 주석, 신규 파일 전혀 없음
- 현재 Auth 구현은 이메일/패스워드 기반 JWT 인증(`register`, `login` 엔드포인트)으로 **완성 상태**
- `realtime_transcription_service.py`는 Auth 레이어와 완전히 독립적 — 의존성 없음

---

## 테스트 실행 결과

`tests/` 디렉토리에 `test_realtime_*` 또는 realtime 키워드를 포함한 테스트 파일이 존재하지 않음.

현재 존재하는 테스트 파일:
- `test_audio_analysis_service.py`
- `test_audio_chunking.py`
- `test_audio_routes.py`
- `test_auth_service.py`
- `test_chunk_builder.py`
- `test_chunk_metadata_service.py`
- `test_context_chunk_planning_service.py`
- `test_embedding_service.py`
- `test_search_chunk_builder.py`
- `test_summary_service.py`
- `test_transcript_ingestion_service.py`
- `test_transcription_service.py`

플랜에서도 realtime 테스트 없음이 확인된 사항과 일치한다.

---

## 프론트엔드 연동 시 주의사항

1. **오디오 포맷 일치 필수**: 프론트엔드에서 반드시 `Audio.RecordingOptionsPresets.HIGH_QUALITY`
   프리셋을 사용해야 한다. 해당 프리셋이 m4a를 출력하는 것을 전제로 suffix를 맞춘 것이므로,
   다른 프리셋을 사용하면 포맷 불일치가 재발할 수 있다.

2. **플랫폼별 포맷 확인**: expo-av `HIGH_QUALITY` 프리셋은 iOS/Android 모두 m4a(AAC) 출력이
   보장되지만, 커스텀 `RecordingOptions`를 사용할 경우 플랫폼별 출력 포맷을 반드시 확인해야 한다.
   Web 환경(Expo Web)에서는 webm이 출력되므로 별도 처리가 필요하다.

3. **WebSocket 바이너리 전송**: 청크는 `ArrayBuffer` (binary frame)로 전송해야 한다.
   Base64 인코딩된 텍스트 프레임은 지원하지 않는다.
