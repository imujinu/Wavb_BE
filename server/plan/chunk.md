# LLM 기반 맥락 단위 Chunking 수정 계획

## Summary
기존 `chunks` 생성 방식은 segment를 길이/시간/화자 변경 기준으로 단순 병합한다. 이 방식은 “요약자료 생성을 위한 맥락 단위”라는 목적과 맞지 않는다. 앞으로는 segments 목록을 LLM에 전달해 **주제 흐름, 질문-답변, 결정/논의 단위, 개념 설명 단위**를 기준으로 chunk 경계를 먼저 계획하고, 그 결과대로 `chunks`를 생성한다.

길이 기준은 chunk를 만드는 주된 기준이 아니라, LLM 실패나 과도하게 긴 chunk를 막는 **fallback/safety guard**로만 사용한다.

최종 계획 문서는 구현 단계에서 `server/plan/chunks.md`로 생성한다.

## Step 1. Chunk 목적 재정의
필요성:
- chunks는 검색용 조각이 아니라, 추후 요약자료 생성을 위한 맥락 단위 저장소다.
- 길이 기준 chunking은 segments를 단순히 붙이는 것과 큰 차이가 없다.

작업:
- `chunk_builder.py` 주석과 metadata를 요약자료 생성 목적에 맞게 정리한다.
- `chunk_goal` 값을 변경한다.
  - meeting: `summary_context_meeting`
  - lecture: `summary_context_lecture`
- 기존 `/rag/query`는 이번 범위에서 제거하지 않고 보류 유지한다.

결과:
- chunks의 역할이 “summary context unit”으로 명확해진다.

## Step 2. LLM Chunk Plan 서비스 추가
필요성:
- 맥락 단위 경계는 단순 길이/화자 변경으로 판단하기 어렵다.
- segments의 의미 흐름을 보고 group을 만들어야 한다.

작업:
- 새 서비스 `ContextChunkPlanningService`를 추가한다.
- 입력:
  - `domain_type`
  - ordered `SegmentCreate` 목록
  - 각 segment의 `segment_index`, `start_seconds`, `end_seconds`, `speaker_label`, `text`
- 출력:
  - chunk group 목록
  - 각 group의 `segment_start_index`
  - `segment_end_index`
  - `title/topic`
  - `reason`
  - `summary_hint`
- LLM 응답은 JSON object로 강제한다.
- LLM 실패, JSON 파싱 실패, 잘못된 segment range가 있으면 fallback builder를 사용한다.

결과:
- chunk 경계가 “길이”가 아니라 “맥락 흐름” 중심으로 결정된다.

## Step 3. 도메인별 Chunk Plan Prompt 설계
필요성:
- meeting과 lecture는 맥락 단위가 다르다.
- 같은 segment라도 회의는 논의/결정 단위, 강의는 개념/설명 단위로 묶어야 한다.

작업:
- meeting prompt 기준:
  - 하나의 안건, 논의 흐름, 질문-답변, 결정/액션 아이템 단위로 묶는다.
  - 짧은 맞장구나 보조 발화는 앞뒤 맥락에 포함한다.
  - 화자 변경만으로 chunk를 나누지 않는다.
- lecture prompt 기준:
  - 하나의 개념, 소주제, 정의-예시-결론 흐름 단위로 묶는다.
  - 예시는 해당 개념 설명 chunk에 포함한다.
  - 새 개념으로 넘어갈 때 chunk를 나눈다.
- 공통 제약:
  - segment index는 누락/중복 없이 순서대로 포함한다.
  - 한 chunk는 연속된 segment range만 포함한다.
  - 너무 짧은 chunk를 만들지 않는다.
  - 너무 긴 chunk가 생기면 의미가 덜 깨지는 지점에서 나눈다.

결과:
- chunks가 요약자료 생성에 바로 사용할 수 있는 문맥 단위로 저장된다.

## Step 4. Builder 구조 변경
필요성:
- 현재 `MeetingChunkBuilder`, `LectureChunkBuilder`가 직접 split을 결정한다.
- 앞으로는 LLM chunk plan을 받아 `ChunkCreate`로 변환하는 역할이 중심이 되어야 한다.

작업:
- `TranscriptIngestionService` 흐름을 변경한다.
  - STT
  - segments 저장
  - LLM context chunk plan 생성
  - plan 기반 chunks 생성
  - chunk metadata enrich
  - chunks 저장
- `chunk_builder.py`는 두 역할로 나눈다.
  - LLM plan을 `ChunkCreate`로 변환하는 builder
  - LLM 실패 시 사용하는 deterministic fallback builder
- fallback 기준은 주 기준이 아니라 안전장치로만 둔다.
  - meeting fallback: 1개 안건처럼 보이는 연속 segment를 최대 180초까지만 허용
  - lecture fallback: 1개 개념 흐름을 최대 300초까지만 허용
- chunk metadata에는 다음을 저장한다.
  - `chunk_goal`
  - `planning_method`: `llm` 또는 `fallback`
  - `planning_reason`
  - `segment_count`

결과:
- 정상 경로에서는 LLM이 맥락 단위 chunk 경계를 결정한다.
- 장애 상황에서도 기존처럼 chunk 저장이 실패하지 않는다.

## Step 5. Metadata Precompute와 역할 분리
필요성:
- 현재 `ChunkMetadataService`는 이미 만들어진 chunk에 topic/summary/keywords를 붙인다.
- 맥락 경계 결정과 metadata 생성은 다른 작업이다.

작업:
- `ContextChunkPlanningService`는 chunk boundary만 결정한다.
- `ChunkMetadataService`는 기존처럼 생성된 chunk의 topic, keywords, summary, domain metadata만 채운다.
- metadata 생성 실패는 기존처럼 chunk 저장 실패로 처리하지 않는다.

결과:
- “어디서 자를지”와 “자른 chunk를 어떻게 설명할지”가 분리된다.

## Step 6. 테스트 계획
필요성:
- chunk가 길이 단위가 아니라 맥락 단위로 묶이는지 검증해야 한다.

작업:
- `tests/test_context_chunk_planning_service.py`
  - meeting segments를 안건별 group plan으로 파싱하는지 확인.
  - lecture segments를 개념별 group plan으로 파싱하는지 확인.
  - 잘못된 JSON 또는 잘못된 segment range면 fallback으로 전환되는지 확인.
- `tests/test_chunk_builder.py`
  - LLM plan의 segment range가 `ChunkCreate.segment_start_index/end_index`로 보존되는지 확인.
  - `planning_method`, `planning_reason`, `segment_count` metadata가 저장되는지 확인.
- `tests/test_transcript_ingestion_service.py`
  - segments 수십 개 입력 시 LLM plan 기준으로 여러 chunks가 저장되는지 확인.
  - LLM planner 실패 시 fallback chunks가 저장되는지 확인.
  - metadata enrich 이후에도 chunk range가 유지되는지 확인.

실행 명령:
```bash
uv run pytest tests/test_context_chunk_planning_service.py tests/test_chunk_builder.py tests/test_transcript_ingestion_service.py
```

## 전체 동작 흐름
```text
/audio/transcripts 요청
→ STT 실행
→ transcripts.full_text 저장
→ segments 저장
→ segments 전체를 LLM planner에 전달
→ 맥락 단위 chunk plan 생성
→ plan을 ChunkCreate 목록으로 변환
→ chunk metadata precompute
→ chunks 저장
→ 이후 요약자료 생성 시 chunks를 순서대로 사용
```

## Assumptions
- chunks는 검색용이 아니라 요약자료 생성을 위한 맥락 단위 저장소다.
- 길이, 시간, segment count는 chunking의 주 기준이 아니라 fallback/safety guard다.
- LLM planner는 기존 `OPENAI_SUMMARY_MODEL`을 재사용한다.
- planner 실패 시 transcript 처리는 실패시키지 않고 fallback chunks를 저장한다.
- 기존 `/rag/query` API는 이번 작업에서 제거하지 않는다.
