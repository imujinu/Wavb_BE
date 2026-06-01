# 실시간 음성 전사 기능 구현 플랜

---

## 서비스 동작 흐름 (완성 후)

### 1. 실시간 녹음 세션 (WebSocket)
```
WS /audio/realtime?token=<JWT>
  → JWT 검증 (쿼리 파라미터)
  → WebSocket accept
  → {"type": "ready"} 전송

  [루프 - 녹음 중]
  클라이언트: 3초 단위 WebM/Opus binary 전송
    → RealtimeTranscriptionService.transcribe_chunk(bytes, chunk_index, accumulated_text)
      → tempfile에 bytes 저장
      → OpenAI Whisper-1 호출 (prompt=이전 전사 텍스트 200자)
      → {"type": "transcript", "chunk_index": N, "text": "...", "start_seconds": N} 전송
      → 실패 시 {"type": "error", "chunk_index": N, "message": "..."} 전송 (세션 유지)
  
  클라이언트: {"type": "stop"} 전송 → 세션 종료
  (클라이언트 화면에는 수신된 텍스트 세그먼트가 실시간 표시되며 사용자가 편집 가능)
```

### 2. 편집 완료 후 저장 (REST)
```
POST /audio/transcripts/realtime  Authorization: Bearer <JWT>
  body: { domain_type, title, duration_seconds, segments: [{segment_index, start_seconds, end_seconds, text}] }
  → TranscriptIngestionService.ingest_from_segments()
    → create_transcript (status: "processing")
    → insert_segments
    → _build_chunks (LLM 청킹 또는 fallback)
    → _enrich_chunks (topic, keywords, summary)
    → insert_chunks
    → _build_and_index_search_chunks (형태소 분석 + 임베딩 + DB 저장)
    → update_transcript_result (status: "completed")
  → 201 { transcript_id, segment_count }
```

---

## Step별 구현 계획

### Step 1: 스키마 정의
**작업:** `server/schemas/realtime.py` 신규 생성

**신규 파일:** `server/schemas/realtime.py`

포함 모델:
- `RealtimeTranscriptEvent` — 서버→클라이언트 WebSocket 이벤트
  ```python
  type: Literal["ready", "transcript", "error"]
  chunk_index: int | None
  text: str | None
  start_seconds: float | None
  message: str | None
  ```
- `RealtimeSegmentInput` — 저장 요청 내 세그먼트 단위
  ```python
  segment_index: int
  start_seconds: float
  end_seconds: float
  text: str  # 사용자가 편집한 최종 텍스트
  ```
- `RealtimeSaveRequest` — `POST /audio/transcripts/realtime` 요청 바디
  ```python
  domain_type: Literal["meeting", "lecture"]
  title: str | None
  duration_seconds: float | None
  segments: list[RealtimeSegmentInput]
  ```
- `RealtimeSaveResponse` — 저장 응답
  ```python
  transcript_id: UUID
  segment_count: int
  ```

**필요성:** 기존 `schemas/rag.py`와 실시간 기능의 스키마를 분리해 독립적으로 관리. 기존 파일에 영향 없음.

---

### Step 2: RealtimeTranscriptionService 구현
**작업:** `server/services/realtime_transcription_service.py` 신규 생성

**신규 파일:** `server/services/realtime_transcription_service.py`

핵심 메서드:
```python
async def transcribe_chunk(
    self,
    audio_bytes: bytes,
    chunk_index: int,
    accumulated_text: str = "",
) -> RealtimeTranscriptEvent
```

**처리 흐름:**
1. `tempfile.NamedTemporaryFile(suffix=".webm", delete=False)` 에 `audio_bytes` 저장
2. `client.audio.transcriptions.create(model="whisper-1", language="ko", response_format="text", prompt=accumulated_text[-200:])`
3. 성공 → `RealtimeTranscriptEvent(type="transcript", ...)` 반환
4. 실패 → `RealtimeTranscriptEvent(type="error", ...)` 반환 (예외 전파 X)
5. `finally` 블록에서 임시 파일 삭제

**`response_format="text"` 선택 이유:** 실시간 전사는 타이밍 정보 없이 텍스트만 필요. `verbose_json` 대비 응답이 가볍고 파싱이 단순해 지연이 줄어듦. 타이밍은 클라이언트가 chunk_index × chunk_duration으로 계산 가능.

**`prompt` 파라미터 사용 이유:** Whisper-1은 직전 문맥을 `prompt`로 받으면 문맥에 맞는 단어를 선택하고 경계 구간의 오인식을 줄임. 200자로 제한하는 이유는 Whisper의 prompt 토큰 제한과 API 비용 균형.

**오류를 예외 전파하지 않는 이유:** 개별 청크 실패가 녹음 세션 전체를 끊으면 안 됨. 클라이언트는 `type="error"` 이벤트를 받고 해당 구간만 빈 칸으로 표시하거나 재시도 UI를 제공할 수 있음.

**WebM 포맷 직접 수신 이유:** 브라우저 MediaRecorder API의 기본 출력 포맷이 WebM/Opus. Whisper-1이 WebM을 직접 지원하므로 FFmpeg 변환 없이 bytes를 그대로 전달 가능 → 처리 지연 감소.

**필요성:** WebSocket 라우트에서 오디오 처리 로직을 분리해 단위 테스트 가능하게 만들고, 라우트가 전송 계층에만 집중하도록 함.

---

### Step 3: TranscriptIngestionService에 ingest_from_segments 추가
**작업:** `server/services/transcript_ingestion_service.py` 수정

**신규 메서드:** `ingest_from_segments()`
```python
async def ingest_from_segments(
    self,
    segments: list[SegmentCreate],
    domain_type: DomainType,
    title: str | None,
    duration_seconds: float | None,
    user_id: UUID | None,
) -> TranscriptIngestionResult
```

**흐름:**
1. `create_transcript(TranscriptCreate(status="processing", ...))`
2. `full_text = " ".join(s.text for s in segments)`
3. `insert_segments(transcript_id, segments)`
4. `_build_chunks(domain_type, segments)` — 기존 메서드 재사용
5. `_enrich_chunks(chunks)` — 기존 메서드 재사용
6. `insert_chunks(transcript_id, chunks)`
7. `_build_and_index_search_chunks(transcript_id, segments)` — 기존 메서드 재사용
8. `update_transcript_result(status="completed", full_text=full_text, ...)`

**기존 `ingest_upload()` 리팩토링:**
- STT 처리 후 `SegmentCreate` 리스트로 변환하는 로직은 그대로 유지
- 변환 완료 후 `ingest_from_segments()` 를 호출하도록 내부 위임
- 외부 API 시그니처(`ingest_upload()`) 변경 없음 → 기존 `POST /audio/transcripts` 동작 무손상

**필요성:** STT 단계를 건너뛰고 기존 후처리 파이프라인(청킹, 임베딩, RAG 인덱싱)을 재사용하려면 진입점이 필요. 코드 중복 없이 두 경로가 동일한 DB/RAG 처리를 공유.

---

### Step 4: 실시간 라우트 구현
**작업:** `server/routes/realtime.py` 신규 생성

**신규 파일:** `server/routes/realtime.py`

포함 엔드포인트:

**① `WS /audio/realtime`**
```
쿼리 파라미터: token (JWT access token)
```
- `AuthService.decode_access_token(token)` 실패 시 `websocket.close(code=4001)` 후 반환
- `websocket.accept()` → `{"type":"ready"}` 전송
- 수신 루프: binary → `transcribe_chunk()` → 결과 전송
- `{"type":"stop"}` JSON 수신 시 루프 종료
- `WebSocketDisconnect` 예외는 정상 종료로 처리

**② `POST /audio/transcripts/realtime`**
```
인증: Depends(get_current_user) — Authorization: Bearer <JWT>
바디: RealtimeSaveRequest
응답: RealtimeSaveResponse (201)
```
- `RealtimeSegmentInput` → `SegmentCreate` 변환 후 `ingest_from_segments()` 호출

**WebSocket JWT를 쿼리 파라미터로 받는 이유:** WebSocket 핸드셰이크 시 브라우저는 커스텀 헤더 설정이 불가. `Authorization: Bearer` 헤더를 쓸 수 없으므로 쿼리 파라미터로 토큰을 전달하는 것이 표준 패턴. HTTPS/WSS 연결 시 쿼리 파라미터도 TLS로 암호화되므로 보안에 문제 없음.

**필요성:** 실시간 전사와 저장을 기존 `audio.py` 에 추가하면 파일이 비대해지고 관심사가 섞임. 별도 파일로 분리해 기능 경계를 명확히 함.

---

### Step 5: main.py 라우터 등록
**작업:** `server/main.py` 수정

```python
from routes.realtime import router as realtime_router
app.include_router(realtime_router)
```

**필요성:** FastAPI에 WebSocket 라우트가 포함된 라우터를 등록해야 엔드포인트가 활성화됨.

---

## 파일 목록 요약

### 신규 생성 (3개)
| 파일 | 역할 |
|------|------|
| `server/schemas/realtime.py` | WebSocket 이벤트 및 저장 요청/응답 스키마 |
| `server/services/realtime_transcription_service.py` | 오디오 bytes → Whisper-1 전사 |
| `server/routes/realtime.py` | WS /audio/realtime, POST /audio/transcripts/realtime |

### 수정 (2개)
| 파일 | 변경 내용 |
|------|-----------|
| `server/services/transcript_ingestion_service.py` | `ingest_from_segments()` 추가, `ingest_upload()` 내부 위임 리팩토링 |
| `server/main.py` | `realtime_router` include 추가 |

### DB 변경 없음
기존 `transcripts`, `segments`, `chunks`, `search_chunks` 테이블을 그대로 사용.

---

## 재사용 컴포넌트

| 컴포넌트 | 위치 | 재사용 방식 |
|----------|------|-------------|
| `AuthService.decode_access_token()` | `server/services/auth_service.py` | WebSocket JWT 검증 |
| `RagRepository` 전체 | `server/repositories/rag_repository.py` | 세그먼트/청크/임베딩 DB 저장 |
| `TranscriptIngestionService._build_chunks()` | `server/services/transcript_ingestion_service.py` | 청크 빌딩 재사용 |
| `TranscriptIngestionService._enrich_chunks()` | 동일 | 메타데이터 enrichment 재사용 |
| `TranscriptIngestionService._build_and_index_search_chunks()` | 동일 | RAG 인덱싱 재사용 |
| `get_current_user`, `get_rag_repository` | `server/dependencies/auth.py` | DI 의존성 재사용 |
| `get_settings()` | `server/settings.py` | OpenAI API 키 및 STT 모델명 |

---

## 검증 방법

```bash
cd server

# Step 3 리팩토링 회귀 테스트 (기존 ingestion 동작 무손상 확인)
uv run pytest tests/test_transcript_ingestion_service.py -v

# Step 2 단위 테스트 실행
uv run pytest tests/test_realtime_transcription_service.py -v

# 서버 실행
uv run uvicorn main:app --reload

# WebSocket 연결 테스트 (wscat 설치 필요: npm install -g wscat)
# 1. 토큰 발급
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"password123"}' | jq -r .access_token)

# 2. WebSocket 연결 (ready 이벤트 수신 확인)
wscat -c "ws://localhost:8000/audio/realtime?token=$TOKEN"

# 3. 저장 엔드포인트 테스트
curl -X POST http://localhost:8000/audio/transcripts/realtime \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "domain_type": "meeting",
    "title": "테스트 회의",
    "duration_seconds": 10.0,
    "segments": [
      {"segment_index": 0, "start_seconds": 0.0, "end_seconds": 5.0, "text": "안녕하세요"},
      {"segment_index": 1, "start_seconds": 5.0, "end_seconds": 10.0, "text": "반갑습니다"}
    ]
  }'
# → 201 { "transcript_id": "...", "segment_count": 2 }
```
