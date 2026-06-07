# Asyncpg Connection Conflict During Indexing

## Summary

파일 처리 또는 오디오 처리 후 RAG 인덱싱 단계에서 다음 에러가 발생할 수 있다.

```text
asyncpg.exceptions._base.InterfaceError: cannot perform operation: another operation is in progress
```

이 에러는 `RagQueryResponse` 스키마 문제가 아니다. 파일/오디오에서 텍스트를 추출한 뒤, segment/chunk/search index/embedding을 저장하는 인덱싱 파이프라인에서 같은 asyncpg connection을 병렬 task들이 동시에 사용해서 발생한다.

## Impact

현재 구조에서는 오디오와 문서/스크립트 계열 입력 모두 영향을 받을 수 있다.

- 오디오: STT로 텍스트와 segment를 만드는 단계는 성공할 수 있다.
- 문서/스크립트: 텍스트 추출과 segment 생성 단계는 성공할 수 있다.
- 공통 문제 지점: 추출된 segment를 RAG 검색용 chunk/search chunk/embedding으로 저장하는 인덱싱 단계에서 실패할 수 있다.

즉, "텍스트 추출 자체가 항상 안 된다"가 아니라, 텍스트 추출 후 임베딩 저장까지 이어지는 인덱싱 완료가 실패할 수 있는 상태다. 실패하면 transcript row에는 텍스트가 남아 있을 수 있지만, 검색용 embedding/search index가 완성되지 않아 RAG 검색에서 해당 자료를 제대로 찾지 못할 수 있다.

## Error Flow

1. `TranscriptProcessingService.process()`가 파일 처리를 시작한다.
2. `_prepare_content()`에서 오디오 STT 또는 문서 텍스트 추출을 수행한다.
3. `_index_content()`가 `TranscriptIngestionService.build_index_for_segments()`를 호출한다.
4. `_run_pipeline()`이 segment 저장, chunk 생성, chunk metadata 생성, search chunk 생성, embedding 저장을 순서대로 수행한다.
5. `ChunkMetadataService.enrich_chunks()`가 여러 chunk를 `asyncio.gather()`로 병렬 처리한다.
6. 각 `_enrich_chunk()` task 안에서 `raise_if_cancel_requested(cancellation_checker)`가 호출된다.
7. cancellation checker가 `RagRepository.is_processing_cancel_requested()`를 호출한다.
8. 이 DB 조회들이 같은 asyncpg connection을 동시에 사용하면서 충돌한다.

## Relevant Code Paths

- `server/services/files/transcript_processing_service.py`
  - `_index_content()`
  - `_cancellation_checker()`
- `server/services/audio/transcript_ingestion_service.py`
  - `build_index_for_segments()`
  - `_run_pipeline()`
  - `_enrich_chunks()`
- `server/services/chunks/chunk_metadata_service.py`
  - `enrich_chunks()`
  - `_enrich_chunk()`
- `server/repositories/rag_repository.py`
  - `is_processing_cancel_requested()`

## Root Cause

취소 기능 자체는 필요한 기능이다. 문제는 취소 확인 위치가 너무 깊다는 점이다.

현재는 chunk metadata를 병렬로 생성하는 개별 task 내부에서 DB 기반 cancellation check를 수행한다. 하지만 asyncpg의 단일 connection은 동시에 여러 query를 실행할 수 없다.

따라서 병렬 task들이 같은 repository/connection으로 동시에 `fetchrow()`를 호출하면 `another operation is in progress` 에러가 발생한다.

## Suggested Fix

DB 기반 cancellation check를 개별 병렬 chunk task 내부에서 제거하고, 파이프라인 checkpoint 단위에서만 수행한다.

권장 위치:

- chunk metadata 병렬 처리 시작 전
- `asyncio.gather()` 완료 후
- segment 저장 전후
- chunk 저장 전후
- search chunk/embedding 생성 전후

구체적으로는 `ChunkMetadataService._enrich_chunk()` 내부의 다음 호출을 제거하거나 DB를 호출하지 않는 방식으로 바꾼다.

```python
await raise_if_cancel_requested(cancellation_checker)
```

`enrich_chunks()` 시작 전/후에는 이미 cancellation check를 둘 수 있으므로, cancellation 기능은 유지하면서 DB connection 동시 사용 문제를 피할 수 있다.

## Alternative Fix

취소 확인을 병렬 task 내부에 유지해야 한다면 cancellation checker 내부에 `asyncio.Lock`을 두어 DB 조회만 직렬화할 수 있다.

다만 이 방식은 병렬 task 내부에서 DB connection에 계속 의존하게 되므로, 현재 구조에서는 checkpoint 방식이 더 단순하고 안전하다.

## Acceptance Criteria

- 오디오 파일 처리 후 STT 결과가 segment/chunk/search chunk/embedding까지 정상 저장된다.
- 문서/스크립트 처리 후 텍스트 추출 결과가 segment/chunk/search chunk/embedding까지 정상 저장된다.
- chunk metadata 병렬 처리 중 `cannot perform operation: another operation is in progress` 에러가 발생하지 않는다.
- 사용자가 처리 취소를 요청하면 주요 파이프라인 단계 사이에서 정상적으로 중단된다.
- 관련 테스트가 추가된다.

## Test Ideas

- `ChunkMetadataService.enrich_chunks()`에 여러 chunk를 넘기고, cancellation checker가 동시에 호출되면 실패하는 fake checker를 주입해 기존 문제를 재현한다.
- 수정 후에는 병렬 task 내부에서 cancellation checker가 동시에 호출되지 않는지 검증한다.
- `TranscriptProcessingService._index_content()`가 audio/document segments 양쪽에서 동일한 인덱싱 파이프라인을 통과하는지 확인한다.
