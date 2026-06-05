# 파일 업로드 텍스트 추출 + RAG 인덱싱 플랜

## Summary

- 새 `files` 라우터를 추가해 `pdf`, `ppt/pptx`, 음성 파일 업로드를 한 엔드포인트에서 받는다.
- 음성은 기존 `TranscriptIngestionService.ingest_upload()`를 그대로 재사용한다.
- PDF/PPT는 LangChain 기반 문서 추출 서비스로 텍스트를 뽑고, 추출 텍스트를 segment로 변환해 기존 chunk/embedding 저장 파이프라인을 재사용한다.
- 페이지, 슬라이드, 음성 시간 위치는 범위 검색에 대비해 nullable 전용 컬럼으로 저장한다.
- 음성 업로드에서 사용자가 특정 언어를 선택하는 입력은 제거하고 전사 서비스의 기본 동작을 따른다.

## Key Changes

- 새 API: `POST /files/upload`
  - `multipart/form-data`
  - fields:
    - `file`: 업로드 파일 필수
    - `file_uri`: 선택, 없으면 `upload://{filename}` 사용
    - `file_name`: 선택, 없으면 `file.filename` 사용
  - 지원 확장자:
    - audio: `.m4a`, `.mp3`, `.wav`, `.webm`
    - document: `.pdf`, `.ppt`, `.pptx`
  - 응답:
    - `transcript_id`
    - `source_type`: `audio | document`
    - `transcript`
    - `segment_count`
    - `chunk_count`
    - `status`

- 새 서비스: `FileIngestionService`
  - 파일 타입 판별 후 audio/document 경로로 분기한다.
  - audio는 기존 `TranscriptIngestionService`에 위임한다.
  - document는 `DocumentTextExtractionService`로 텍스트 추출 후 `TranscriptIngestionService.ingest_from_segments()` 계열을 재사용한다.
  - 문서 segment는 페이지/슬라이드 단위로 생성하고, 시간값은 `segment_index` 기반 가짜 범위로 채운다. 예: `0.0~1.0`, `1.0~2.0`.
  - 문서 segment 생성 시 PDF 페이지 또는 PPT 슬라이드 위치를 source range 컬럼 값으로 함께 채운다.

- 기존 음성 업로드 로직 수정
  - `/audio/transcripts`의 `languages` form field를 제거한다.
  - `validate_languages()`와 `ALLOWED_LANGUAGE_SETS`를 제거한다.
  - `TranscriptIngestionService.ingest_upload()`은 `languages` 인자를 받지 않는다.
  - `TranscriptionService.transcribe_with_segments(file)`처럼 언어 인자를 넘기지 않고 서비스 기본값에 맡긴다.
  - 기존 테스트에서 `languages` 입력/검증 기대값을 제거한다.

- DB migration 추가
  - `segments`, `chunks`, `search_chunks`에 동일한 source range 컬럼을 추가한다.
  - 추가 컬럼:
    - `source_type TEXT`
    - `source_page_start INT`
    - `source_page_end INT`
    - `source_slide_start INT`
    - `source_slide_end INT`
    - `source_start_seconds NUMERIC`
    - `source_end_seconds NUMERIC`
  - PDF row는 `source_type='pdf'`와 page 컬럼만 채우고 나머지는 `NULL`로 둔다.
  - PPT/PPTX row는 `source_type='ppt'`와 slide 컬럼만 채우고 나머지는 `NULL`로 둔다.
  - 음성 row는 `source_type='audio'`와 seconds 컬럼만 채우고 나머지는 `NULL`로 둔다.
  - nullable 컬럼이 많은 구조는 의도된 다형 source 위치 모델로 본다.
  - chunk/search_chunk는 포함된 segment들의 최소/최대 page, slide, seconds 범위를 전파한다.

- 새 서비스: `DocumentTextExtractionService`
  - LangChain `PyPDFLoader`로 PDF 텍스트 추출.
  - `.pptx`는 가벼운 의존성을 위해 `python-pptx` 기반 커스텀 LangChain loader로 슬라이드 텍스트 추출.
  - `.ppt`는 LibreOffice 변환 설정이 있을 때만 임시 `.pptx`로 변환 후 추출한다.
  - LibreOffice가 없으면 `.ppt`는 `422 Legacy PPT conversion is not configured.`로 거절한다.

- 의존성 추가: `server/pyproject.toml`
  - `langchain-core`
  - `langchain-community`
  - `pypdf`
  - `python-pptx`

- `server/main.py`
  - `files_router` include 추가.

## Public Interfaces

- 기존 `/audio/transcripts`는 그대로 유지한다.
- 기존 `/audio/transcripts` 요청에서는 `languages` form field를 더 이상 받지 않는다.
- 신규 `/files/upload`가 권장 범용 업로드 경로가 된다.
- 기존 RAG 검색 API는 변경하지 않는다.
- 페이지, 슬라이드, 음성 시간 위치는 `segments`, `chunks`, `search_chunks`의 source range 컬럼에 저장한다.
- 문서 파일 종류 구분은 기존 `transcripts.mime_type`, `original_filename`을 사용한다.

## Test Plan

- 라우트 테스트
  - PDF 업로드가 `200`과 `transcript_id`를 반환한다.
  - PPTX 업로드가 `200`과 `segment_count`를 반환한다.
  - 음성 업로드는 기존 ingestion service로 위임된다.
  - `/audio/transcripts`와 `/files/upload`는 음성 업로드 시 `languages` 없이 성공한다.
  - 지원하지 않는 확장자는 `400`.
  - LibreOffice 미설정 `.ppt`는 `422`.

- 서비스 테스트
  - PDF loader 결과가 페이지별 `SegmentCreate`로 변환된다.
  - PPTX loader 결과가 슬라이드별 `SegmentCreate`로 변환된다.
  - PDF segment는 `source_type='pdf'`와 page 범위를 저장한다.
  - PPTX segment는 `source_type='ppt'`와 slide 범위를 저장한다.
  - 빈 텍스트 문서는 `422 Document text extraction result is empty.` 처리한다.
  - 문서 ingestion도 chunks/search_chunks/embedding 저장 흐름을 호출하고 source range 컬럼을 전파한다.

- DB/repository 테스트
  - `insert_segments`, `insert_chunks`, `insert_search_chunks`가 source range 컬럼을 저장한다.
  - PDF/PPT/audio 각각 해당 타입 컬럼만 채우고 나머지 source range 컬럼은 `NULL`로 둔다.
  - 기존 음성 ingestion은 page/slide 컬럼이 `NULL`이어도 깨지지 않는다.

- 회귀 테스트
  - 기존 `test_audio_routes.py`
  - 기존 `test_transcript_ingestion_service.py`
  - 신규 `test_file_routes.py`
  - 신규 `test_document_text_extraction_service.py`

## Assumptions

- 처리 방식은 동기 완료 방식으로 구현한다.
- 원본 파일 자체는 서버 파일스토리지에 영구 저장하지 않고, 기존처럼 `file_uri` 메타데이터만 저장한다.
- `.ppt` 지원은 선택된 요구를 반영하되, 가벼운 기본 스택을 유지하기 위해 LibreOffice 변환이 가능한 환경에서만 활성화한다.
- 기존 `transcripts.source_audio_uri` 컬럼명은 이번 범위에서 바꾸지 않는다.
- 위치 정보는 향후 범위 조건 검색에 사용할 가능성이 있으므로 JSONB 대신 nullable 전용 컬럼으로 저장한다.
