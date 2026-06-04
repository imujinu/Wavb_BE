# Knowledge Graph Graph-Node Build 계획

## 1. 요약

오디오/PDF/PPT 강의 자료에서 개념 노드와 관계 엣지를 추출해 Neo4j에 저장하는 **Agent 1: graph build** 기능을 구현한다.

이번 계획의 핵심 결정은 다음과 같다.

- LangGraph는 사용하지 않는다.
- Agent 1, Agent 2, Agent 3은 하나의 graph state를 공유하지 않고, 각각 독립 service/agent로 구성한다.
- Agent 1은 그래프 생성만 담당한다.
- Agent 2는 나중에 Neo4j graph를 조회해 퀴즈를 생성한다.
- Agent 3은 나중에 퀴즈 답변과 학습 이력을 분석해 복습 일정을 만든다.
- Agent 2/3은 Agent 1의 내부 pipeline state를 재사용하지 않고 `graph_id`, `user_id`, Neo4j query 결과만 사용한다.
- 그래프 노드/엣지에는 반드시 원본 근거 위치를 저장한다.
- 근거 위치는 source type별로 다음 구조를 사용한다.

```json
{
  "source_type": "audio",
  "location": [
    {"type": "timestamp", "value": "00:12:34"}
  ]
}
```

```json
{
  "source_type": "pdf",
  "location": [
    {"type": "page", "value": 12}
  ]
}
```

`ppt`, `pptx`는 API 입력과 저장 모두 `source_type="ppt"`로 정규화한다.

## 2. 프로젝트 구조

추가할 구조는 다음과 같다.

```text
server/
  db/
    neo4j.py
  routes/
    graph.py
  schemas/
    graph.py
  services/
    graph/
      graph_build_state.py
      graph_build_service.py
      graph_document_loader_service.py
      graph_text_splitter_service.py
      graph_concept_extraction_service.py
      graph_relation_extraction_service.py
      graph_embedding_service.py
      graph_location_service.py
      graph_progress_service.py
  repositories/
    graph_repository.py
  tests/
    test_graph_build_service.py
    test_graph_location_service.py
    test_graph_repository.py
    test_graph_routes.py
```

역할 분리:

- `graph_build_service.py`: Agent 1 orchestration만 담당한다.
- `graph_location_service.py`: audio timestamp, pdf/ppt page location을 공통 evidence shape로 변환한다.
- `graph_repository.py`: Neo4j Cypher 저장/조회만 담당한다.
- Agent 2/3 관련 파일은 이번 단계에서 만들지 않는다.

## 3. State와 DTO 설계

### 3.1 Agent 공유 state를 만들지 않는다

`Agent1PipelineState`를 Agent 2/3까지 확장하는 방식은 사용하지 않는다. 이유는 Agent 1의 중간 산출물인 chunk, noun candidates, relation extraction 결과가 Agent 2/3의 직접 의존성이 되면 결합도가 커지기 때문이다.

Agent별 입력은 다음처럼 분리한다.

```text
Agent 1 graph build
입력: uploaded file, user_id
출력: graph_id, node_count, edge_count

Agent 2 quiz generation
입력: graph_id, user_id, optional concept_ids
출력: quiz set

Agent 3 answer analysis/review
입력: graph_id, user_id, quiz_session_id, user answers
출력: weak concepts, review schedule
```

### 3.2 GraphBuildState

Agent 1 내부에서만 사용하는 상태는 `GraphBuildState`로 둔다.

```python
class GraphBuildState(TypedDict, total=False):
    build_id: str
    graph_id: str
    user_id: str
    status: Literal["pending", "running", "completed", "failed"]
    current_step: str | None
    progress: float
    warnings: list[str]
    error: str | None

    source_type: Literal["audio", "pdf", "ppt"]
    source_filename: str
    mime_type: str | None

    documents: list[GraphDocument]
    chunks: list[GraphChunk]
    noun_candidates_by_chunk: dict[str, list[str]]
    relations_by_chunk: dict[str, list[ExtractedRelation]]
    concept_embeddings: dict[str, list[float]]
```

### 3.3 Source location schema

모든 source 근거 위치는 공통 DTO로 관리한다.

```python
class SourceLocation(TypedDict):
    type: Literal["timestamp", "page"]
    value: str | int

class SourceEvidence(TypedDict):
    source_type: Literal["audio", "pdf", "ppt"]
    location: list[SourceLocation]
    chunk_id: str
    text_excerpt: str
```

정책:

- audio는 `timestamp`를 사용한다.
- pdf/ppt는 `page`를 사용한다.
- 한 개념 또는 관계가 여러 위치에서 발견되면 `location` 또는 `evidence` 배열로 누적한다.
- frontend D3는 node/edge 클릭 시 `source_type`과 `location`을 표시한다.

## 4. Neo4j schema

### 4.1 Concept node

```text
(:Concept {
  graph_id,
  user_id,
  name,
  normalized_name,
  language,
  embedding,
  source_type,
  evidence,
  source_count,
  created_at,
  updated_at
})
```

`evidence`는 JSON 직렬화 가능한 list로 저장한다.

```json
[
  {
    "source_type": "audio",
    "location": [{"type": "timestamp", "value": "00:12:34"}],
    "chunk_id": "chunk-001",
    "text_excerpt": "역전파는 손실 함수의 기울기를..."
  }
]
```

### 4.2 Relationship

관계 타입은 Neo4j relationship type을 동적으로 늘리지 않고 `RELATES_TO` 하나로 고정한다. 실제 의미는 property `type`에 저장한다.

```text
(:Concept)-[:RELATES_TO {
  graph_id,
  type,
  weight,
  evidence,
  evidence_count,
  model,
  created_at,
  updated_at
}]->(:Concept)
```

관계 의미 타입:

```text
PREREQUISITE_OF
PART_OF
CONTRASTS_WITH
EXPLAINS
RELATED_TO
```

### 4.3 Constraint/index

```cypher
CREATE CONSTRAINT concept_unique IF NOT EXISTS
FOR (c:Concept)
REQUIRE (c.graph_id, c.normalized_name) IS UNIQUE;

CREATE INDEX concept_graph_user IF NOT EXISTS
FOR (c:Concept)
ON (c.graph_id, c.user_id);

CREATE VECTOR INDEX concept_embedding IF NOT EXISTS
FOR (c:Concept)
ON (c.embedding)
OPTIONS {
  indexConfig: {
    `vector.dimensions`: 1536,
    `vector.similarity_function`: 'cosine'
  }
};
```

## 5. Agent 1 단계별 구현 계획

### Step 1. Neo4j 연결 추가

필요성: 그래프 데이터와 traversal query는 Neo4j가 담당해야 한다.

작업:

- `db/neo4j.py`에 Neo4j async driver provider 추가
- settings에 `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` 추가
- FastAPI startup/shutdown에서 driver lifecycle 관리
- 테스트에서는 fake repository 주입 가능하게 구성

### Step 2. Graph API schema 추가

필요성: 업로드, build 결과, D3 graph 응답, source location 응답 구조를 고정한다.

작업:

- `GraphBuildRequest`
- `GraphBuildResponse`
- `GraphNodeResponse`
- `GraphEdgeResponse`
- `GraphDetailResponse`
- `SourceEvidence`
- `SourceLocation`

D3 응답 예시:

```json
{
  "graph_id": "uuid",
  "nodes": [
    {
      "id": "concept:backpropagation",
      "label": "역전파",
      "type": "Concept",
      "weight": 4,
      "source_type": "audio",
      "location": [
        {"type": "timestamp", "value": "00:12:34"}
      ],
      "evidence": []
    }
  ],
  "edges": [
    {
      "id": "edge:1",
      "source": "concept:loss",
      "target": "concept:backpropagation",
      "type": "EXPLAINS",
      "weight": 0.82,
      "source_type": "audio",
      "location": [
        {"type": "timestamp", "value": "00:12:34"}
      ],
      "evidence": []
    }
  ]
}
```

### Step 3. File loading

필요성: 파일 타입별 텍스트 추출을 통합 인터페이스로 다룬다.

작업:

- LangChain document loader는 PDF/PPT에만 사용한다.
- PDF는 page metadata를 유지한다.
- PPT/PPTX는 slide/page metadata를 page로 정규화한다.
- audio는 loader에서 원문 text를 만들지 않고 STT 단계로 넘긴다.
- `source_type`은 `audio | pdf | ppt`만 허용한다.

설정:

- PDF/PPT chunk location은 loader metadata의 page/slide index를 기반으로 한다.
- page 번호는 frontend 표시를 위해 1-based int로 저장한다.

### Step 4. Audio transcription

필요성: audio도 PDF/PPT와 같은 graph chunk pipeline으로 합류해야 한다.

작업:

- 기존 `TranscriptionService` 재사용
- segment의 `start_seconds`를 timestamp location으로 변환
- timestamp format은 `HH:MM:SS`
- chunk가 여러 segment를 포함하면 대표 위치는 첫 segment start timestamp로 둔다.
- 상세 evidence에는 필요한 경우 chunk start/end timestamp를 확장 가능하게 남긴다.

설정 이유:

- 첫 segment timestamp는 사용자가 오디오에서 해당 개념이 처음 등장한 위치로 이동하기 가장 쉽다.

### Step 5. Text splitting

필요성: GPT 관계 추출 비용과 정확도를 제어한다.

작업:

- LangChain `RecursiveCharacterTextSplitter` 사용
- 기본 `GRAPH_CHUNK_SIZE=1200`
- 기본 `GRAPH_CHUNK_OVERLAP=150`
- chunk마다 `chunk_id`, `chunk_index`, `text`, `source_type`, `location`을 유지한다.

source별 chunk location:

```json
{
  "source_type": "audio",
  "location": [{"type": "timestamp", "value": "00:12:34"}]
}
```

```json
{
  "source_type": "pdf",
  "location": [{"type": "page", "value": 12}]
}
```

```json
{
  "source_type": "ppt",
  "location": [{"type": "page", "value": 7}]
}
```

### Step 6. Kiwi noun extraction

필요성: GPT가 임의 개념을 만들지 않게 후보 개념을 제한한다.

작업:

- Kiwi는 명사 후보 추출만 담당한다.
- 다른 NLP 도구로 Kiwi 결과를 재처리하지 않는다.
- chunk별 noun candidate dedupe
- 영어 약어와 숫자가 포함된 기술 용어는 보존한다.
- `GRAPH_MAX_NOUNS_PER_CHUNK=60`
- noun candidate가 2개 미만인 chunk는 GPT relation extraction을 skip한다.
- Kiwi 실패 시 build failed 처리한다.

### Step 7. GPT relation extraction

필요성: 관계 방향과 관계 타입은 단순 명사 추출만으로 알 수 없으므로 GPT가 담당한다.

작업:

- OpenAI SDK 직접 사용
- LangChain prompt/chaining 사용 금지
- 입력은 `chunk_text`, `noun_candidates`, `source_type`, `location`만 전달한다.
- 출력 schema:

```json
{
  "relations": [
    {
      "source": "개념A",
      "target": "개념B",
      "type": "EXPLAINS",
      "weight": 0.82
    }
  ]
}
```

정책:

- `source`, `target`은 noun candidate 안에 있어야 한다.
- 후보 밖 개념은 폐기하고 warning에 남긴다.
- `weight`는 `0.0 ~ 1.0`.
- 같은 chunk 안에서 `(source, target, type)` 중복은 병합한다.
- relation evidence에는 해당 chunk의 `source_type`, `location`, `chunk_id`, `text_excerpt`를 붙인다.

### Step 8. Concept embedding

필요성: Neo4j vector index로 유사 개념 검색과 향후 추천/퀴즈 생성을 지원한다.

작업:

- relation에 등장한 unique concept만 embedding한다.
- 모델은 `text-embedding-3-small`.
- concept noun 단독 문자열을 embedding input으로 사용한다.
- 동일 graph 내 기존 concept embedding이 있으면 재사용한다.
- embedding 실패 시 build failed 처리한다.

### Step 9. Neo4j save

필요성: D3 visualization과 후속 Agent 2/3 query 기반을 만든다.

작업:

- Concept node upsert
- RELATES_TO edge upsert
- node evidence에 개념이 등장한 모든 source location을 누적한다.
- edge evidence에 관계가 추출된 source location을 누적한다.
- 중복 node 기준은 `(graph_id, normalized_name)`.
- 중복 edge 기준은 `(graph_id, source_normalized_name, target_normalized_name, type)`.
- edge weight는 중복 발생 시 max 값을 유지한다.
- evidence는 너무 커지지 않게 node/edge별 최대 10개까지 저장한다.

### Step 10. SSE endpoint 구조만 정의

필요성: 긴 graph build 진행률을 frontend에 표시할 수 있게 API 모양을 먼저 고정한다.

이번 단계에서는 실제 streaming 구현을 하지 않는다.

```text
GET /graphs/builds/{build_id}/events
```

event shape:

```json
{
  "build_id": "uuid",
  "status": "running",
  "step": "extract_relations",
  "progress": 0.55,
  "message": "관계 추출 중",
  "error": null
}
```

## 6. FastAPI endpoint 계획

### Graph build

```text
POST /graphs/builds
Content-Type: multipart/form-data
Auth: required
```

form fields:

- `file`
- `source_type`: `audio | pdf | ppt`, optional
- `title`: optional

response:

```json
{
  "build_id": "uuid",
  "graph_id": "uuid",
  "status": "completed",
  "node_count": 42,
  "edge_count": 58,
  "warnings": []
}
```

### Graph detail for D3

```text
GET /graphs/{graph_id}
Auth: required
```

response:

```json
{
  "graph_id": "uuid",
  "nodes": [],
  "edges": []
}
```

### Build status

```text
GET /graphs/builds/{build_id}
Auth: required
```

response:

```json
{
  "build_id": "uuid",
  "graph_id": "uuid",
  "status": "running",
  "current_step": "extract_relations",
  "progress": 0.5,
  "error": null
}
```

### SSE structure only

```text
GET /graphs/builds/{build_id}/events
Auth: required
```

## 7. 테스트 계획

단위 테스트:

- source type 추론: `.mp3 -> audio`, `.pdf -> pdf`, `.pptx -> ppt`
- audio segment start seconds가 `HH:MM:SS` timestamp location으로 변환되는지
- PDF/PPT page metadata가 `{"type": "page", "value": n}`으로 변환되는지
- Kiwi noun extraction 실패 시 build failed가 되는지
- noun 후보 2개 미만 chunk는 GPT 호출을 skip하는지
- GPT relation이 후보 밖 개념을 반환하면 폐기되는지
- relation evidence에 `source_type`, `location`, `chunk_id`가 포함되는지
- Neo4j node/edge upsert query가 evidence를 누적하는지

통합 테스트:

- PDF fixture → page location 포함 graph 생성
- PPT fixture → page location 포함 graph 생성
- Audio fake STT → timestamp location 포함 graph 생성
- 같은 concept가 여러 위치에서 나오면 node evidence가 누적되는지
- 같은 relation이 여러 위치에서 나오면 edge evidence가 누적되는지
- 다른 user의 graph 조회가 차단되는지

API 테스트:

- `POST /graphs/builds` 성공
- unsupported file type은 `400`
- empty extracted text는 실패
- `GET /graphs/{graph_id}`가 D3 DTO를 반환
- owner mismatch는 `404`

## 8. 주요 리스크와 완화

- 관계 추출 토큰 비용 증가  
  → noun 후보 2개 미만 skip, noun 후보 상한 60개, chunk size 1200으로 제한한다.

- GPT가 후보 밖 개념을 생성  
  → noun candidate membership 검증 후 폐기한다.

- source location 손실  
  → chunk 생성 시점부터 `source_type`과 `location`을 필수 필드로 들고 간다.

- PPT loader metadata 불안정  
  → service layer에서 slide/page metadata를 page로 정규화하고, 없으면 warning 처리한다.

- evidence property가 과도하게 커짐  
  → node/edge별 evidence 최대 10개로 제한한다.

- Agent 간 결합 증가  
  → Agent 2/3은 Agent 1 state를 사용하지 않고 Neo4j graph 조회 결과만 사용한다.

## 9. 최종 동작 흐름

```text
사용자 파일 업로드
 -> source_type 감지 또는 검증
 -> audio: STT + timestamp location 생성
 -> pdf/ppt: LangChain loader + page location 생성
 -> LangChain splitter로 GraphChunk 생성
 -> Kiwi noun 후보 추출
 -> GPT가 후보 명사 간 relation 추출
 -> concept embedding 생성
 -> Neo4j Concept node / RELATES_TO edge upsert
 -> node/edge evidence에 source_type + location 저장
 -> graph_id 반환
 -> frontend가 GET /graphs/{graph_id} 호출
 -> React Native WebView의 D3.js가 nodes/edges와 근거 위치 표시
```

## 10. 산출물

이 문서는 `server/plan/graph.md`에 유지한다. 구현 단계에서는 이 계획을 기준으로 Graph Agent 1 기능을 단계별로 추가한다.
