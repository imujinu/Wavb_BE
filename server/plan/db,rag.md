# 오디오 STT/RAG MVP 아키텍처 플랜

## Summary

- MVP는 `transcripts`, `segments`, `chunks` 3개 레이어만 중심으로 설계한다.
- `transcripts`는 원본 transcript와 업로드/STT 상태의 source of truth 역할을 한다.
- `segments`는 STT 결과의 최소 단위로 speaker, timestamp, text를 보존한다.
- `chunks`는 retrieval 전용 데이터로 topic, keyword, summary, embedding을 가진다.
- 검색은 `metadata filter → keyword/full-text search → 필요 시 vector search` 순서로 수행한다.
- PostgreSQL을 기본 DB로 사용하고, semantic search는 `pgvector`로 시작한다.

## 전체 아키텍처 흐름

```text
오디오 업로드
→ transcripts row 생성(status=uploaded/processing)
→ STT 실행
→ transcripts.full_text 저장
→ segments 저장
→ domain_type에 따라 chunk 생성
→ chunks에 topic/keywords/summary/embedding 저장
→ 사용자 질의
→ metadata filtering
→ keyword/full-text search
→ 필요 시 pgvector semantic search
→ 관련 chunk 기반 응답/요약 생성
```


## Chunk 생성 전략

### 회의록

목표는 factual retrieval이다. “누가”, “언제”, “무엇을 말했는지”, “무슨 결정이 있었는지”를 잘 찾는 것이 중요하다.

- 기본 단위: speaker turn 또는 짧은 발화 묶음
- chunk 크기: 너무 크지 않게 유지, 대략 300~900 tokens
- 분리 기준:
  - speaker 변경
  - topic 전환
  - 결정/액션 아이템 등장
  - timestamp 구간이 지나치게 길어지는 경우
- 병합 기준:
  - 너무 짧은 단답 발화
  - 같은 speaker의 연속 발화
  - 앞뒤 문맥 없이는 의미가 약한 발화
- metadata:
  - `speaker_labels`
  - `topic`
  - `keywords`
  - `decision_flag`
  - `action_item_flag`
  - `start_seconds`, `end_seconds`

회의 chunk는 실제 발언 보존이 중요하므로 `chunks.text`에는 요약문이 아니라 원문 발화 중심 텍스트를 저장한다. `summary`는 검색 결과 표시와 답변 생성 보조용으로만 사용한다.

### 강의

목표는 semantic 이해와 개념 연결이다. 특정 문장보다 개념 흐름과 학습 맥락이 중요하다.

- 기본 단위: topic/chapter/context section
- chunk 크기: 회의보다 크게 유지, 대략 900~1800 tokens
- 분리 기준:
  - 주제 변경
  - 챕터/소단원 전환
  - 정의, 예시, 결론 단위
- overlap:
  - 개념 연결을 위해 10~15% 정도 허용
  - MVP에서는 segment 기준 앞뒤 1~2개 segment overlap으로 단순화
- metadata:
  - `topic`
  - `subtopic`
  - `keywords`
  - `concepts`는 `metadata.concepts`
  - `chapter_index`는 `metadata.chapter_index`

강의 chunk는 원문만으로 검색 품질이 부족할 수 있으므로 embedding 입력은 `topic + summary + text` 조합을 기본값으로 한다.

## Retrieval Pipeline

1. Query 분석
   - `domain_type` 확인
   - speaker, topic, timestamp, transcript_id 같은 metadata 조건 추출
   - 회의는 factual query, 강의는 conceptual query 가능성을 우선 판단

2. Metadata filtering
   - `transcript_id`, `domain_type`, `speaker_labels`, `topic`, `keywords`로 후보 축소
   - 회의 질의에서 speaker가 포함되면 speaker filter를 강하게 적용
   - 강의 질의에서 topic이 포함되면 topic/subtopic filter를 먼저 적용

3. Keyword/full-text search
   - PostgreSQL full-text search로 `chunks.text`, `summary`, `keywords` 검색
   - 회의록의 사람 이름, 실제 표현, 결정 문구 검색에 우선 사용

4. Semantic search
   - 다음 경우에만 pgvector 검색을 수행한다:
     - keyword 결과가 부족한 경우
     - 강의 도메인의 개념형 질문인 경우
     - 사용자가 원문 표현과 다른 말로 질문한 경우
   - vector search는 MVP에서 primary가 아니라 fallback 또는 보조 score로 둔다.

5. 결과 병합
   - metadata match, keyword score, vector similarity를 합산
   - 회의: speaker/topic/timestamp match 가중치 높임
   - 강의: vector similarity/topic continuity 가중치 높임

6. 응답 생성
   - 선택된 chunk text와 summary를 LLM context로 전달
   - 응답에는 가능하면 source timestamp와 speaker/topic을 함께 반환한다.

## Metadata 설계

공통 metadata:

- `topic`: chunk의 대표 주제
- `subtopic`: 세부 주제
- `keywords`: 검색 보조 키워드 배열
- `summary`: chunk 단위 짧은 요약
- `start_seconds`, `end_seconds`: 근거 위치
- `metadata`: 실험적/도메인별 필드 저장용 JSONB

회의 전용 metadata:

- `speaker_labels`
- `metadata.decision_flag`
- `metadata.action_item_flag`
- `metadata.participants`
- `metadata.meeting_date`

강의 전용 metadata:

- `metadata.chapter_index`
- `metadata.concepts`
- `metadata.learning_points`
- `metadata.prerequisite_topics`

Topic/keyword 생성은 chunk 생성 직후 LLM으로 precompute한다. 실패하더라도 chunk text는 저장하고, topic/keyword/summary는 retry 가능한 후처리 작업으로 남긴다.

## 구현 우선순위

### Step 1. DB persistence 기반 추가

필요성:
- 현재 STT 결과가 검색 가능한 영속 데이터로 남지 않으면 RAG를 만들 수 없다.

작업:
- PostgreSQL 연결 설정 추가
- `transcripts`, `segments`, `chunks` migration 작성
- transcript/segment/chunk repository 계층 추가
- pydantic을 사용해 model과 repository 분리

결과:
- 오디오 처리 결과를 DB에 저장할 수 있다.

### Step 2. Upload → STT → transcript 저장 흐름 구현

필요성:
- MVP의 source of truth는 transcript이므로 먼저 안정적으로 저장되어야 한다.

작업:
- 업로드 시 `transcripts` row 생성
- STT 완료 후 `full_text`, `duration_seconds`, `stt_model`, `status` 업데이트
- STT segment를 `segments`에 저장

결과:
- 오디오 1건이 transcript와 segment로 재사용 가능한 상태가 된다.

### Step 3. 회의/강의 chunk builder 분리

필요성:
- 두 도메인의 retrieval 목표가 다르므로 chunking 전략을 초기에 분리해야 한다.

작업:
- `MeetingChunkBuilder` 구현
- `LectureChunkBuilder` 구현
- `domain_type`에 따라 builder 선택
- 생성된 chunk를 `chunks`에 저장

결과:
- 같은 STT 결과라도 회의/강의에 맞는 검색 단위가 생성된다.

### Step 4. Chunk metadata precompute

필요성:
- metadata filtering이 1차 검색이므로 topic/keyword 품질이 retrieval 품질을 크게 좌우한다.

작업:
- chunk별 `topic`, `subtopic`, `keywords`, `summary` 생성
- meeting은 decision/action item metadata 생성
- lecture는 concepts/learning_points metadata 생성

결과:
- metadata 기반 후보 축소와 UI 표시가 가능해진다.

### Step 5. Full-text search 구현

필요성:
- MVP에서 가장 저렴하고 안정적인 검색 품질을 먼저 확보한다.

작업:
- `chunks.text`, `summary`, `keywords` 대상 full-text 검색
- metadata filter와 조합
- `POST /rag/query` 최소 API 추가

결과:
- vector 없이도 기본 질의 검색이 가능하다.

### Step 6. pgvector semantic search 추가

필요성:
- keyword로 찾기 어려운 개념형 질의와 표현 차이를 보완한다.

작업:
- chunk embedding 생성
- `chunks.embedding` 저장
- keyword 결과 부족 시 vector fallback 수행
- lecture 도메인에서는 vector score 비중을 더 높임

결과:
- 회의 factual 검색과 강의 semantic 검색을 모두 지원한다.

### Step 7. 요약/응답 API 정리

필요성:
- 사용자에게 검색 결과만이 아니라 요약 응답을 제공해야 한다.

작업:
- transcript-level summary는 `transcripts.summary`에 저장
- chunk-level summary는 `chunks.summary`에 저장
- 질의 응답은 검색된 chunk 기반으로 생성
- 응답에 source chunk, timestamp, speaker/topic 포함

결과:
- “검색 가능한 transcript 시스템”과 “사용자 질의 기반 검색/요약 응답”이 MVP로 완성된다.

## MVP 기준과 과설계 방지

- 테이블은 `transcripts`, `segments`, `chunks` 3개로 시작한다.
- 별도 vector DB는 도입하지 않는다.
- embedding 전용 테이블은 MVP에서 만들지 않는다.
- speaker diarization 정확도 개선은 후순위로 두고, nullable `speaker_label`만 준비한다.
- 복잡한 reranker, graph RAG, agentic retrieval은 제외한다.
- chunk versioning은 `chunk_strategy` 문자열로만 시작한다.
- 검색 품질 문제가 실제로 확인되기 전까지 vector search를 primary로 올리지 않는다.

## Test Plan

- STT 저장:
  - 오디오 업로드 후 transcript row가 생성되는지 확인
  - STT 완료 후 `full_text`, `status`, `duration_seconds`가 업데이트되는지 확인
  - STT segment가 순서대로 저장되는지 확인

- Chunking:
  - meeting은 speaker/timestamp 중심 chunk가 생성되는지 확인
  - lecture는 topic/context 중심 chunk가 생성되는지 확인
  - chunk가 원본 segment index와 timestamp 범위를 보존하는지 확인

- Metadata:
  - topic, keywords, summary가 chunk에 저장되는지 확인
  - metadata 생성 실패 시 chunk text는 유지되는지 확인

- Retrieval:
  - metadata filter가 speaker/topic 조건을 반영하는지 확인
  - keyword/full-text search가 chunk 후보를 반환하는지 확인
  - vector fallback이 결과 부족 시에만 실행되는지 확인
  - meeting query는 factual source를 잘 반환하는지 확인
  - lecture query는 개념형 질문에 관련 chunk를 반환하는지 확인

## Assumptions

- PostgreSQL과 pgvector를 MVP 기본값으로 사용한다.
- embedding dimension은 사용하는 embedding model에 맞춰 조정하되, 예시는 `vector(1536)` 기준이다.
- MVP에서는 업로드 파일 메타데이터와 처리 상태를 `transcripts`에 포함한다.
- `domain_type`은 업로드 시 사용자가 선택하거나 API 입력으로 받는다.
- 실제 구현 계획 파일을 만들 때는 PLAN.md 규칙에 따라 `server/plan/audio-stt-rag-mvp.md` 같은 별도 markdown으로 저장한다.
