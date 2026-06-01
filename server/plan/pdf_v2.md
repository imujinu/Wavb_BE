# 계획 v2: 긴 스크립트 요약을 truncation → map-reduce 로 교체

## Context (왜 바꾸는가)

v1의 `TemplatedSummaryService.summarize_for_template`는 입력이 길면 이렇게 처리한다:
```python
if len(text) > self._max_input_chars:   # 48000자
    text = text[: self._max_input_chars]   # 뒷부분을 통째로 버림
```
→ 긴 음성일수록 **뒤 내용이 조용히 사라진 채** 요약된다(에러도 없음). 실서비스 품질로 부적절.

### 논의로 확정된 방향
- **검색 청크(`chunks`)/`segments`는 재사용하지 않는다.** 그것들은 *검색용*으로 잘게/손실 있게 만든 데이터고, 문서 요약은 *전체 맥락 보존*이 목적이라 성격이 다르다. → 관심사 분리 유지(요약은 자기 테이블 `summary_documents`에 결과만 저장).
- 문서 요약은 **항상 `full_text` 기반**으로 한다.
- 길어서 한 번에 안 들어가면 **잘라서 비동기 병렬 부분요약 → 합쳐서 템플릿 구조화(map-reduce)**. 중간 부분요약은 **메모리에만** 두고 저장하지 않는다(임시 테이블/DB 없음).
- "요약 = 구조 변환"이므로 **LLM 호출은 불가피**(이어붙이기로 대체 불가). 줄일 수 있는 건 호출 횟수/입력량뿐.

## 변경 범위 (작다 — 사실상 서비스 1파일 + 테스트)

| 파일 | 변경 |
|---|---|
| `server/services/templated_summary_service.py` | truncation 분기를 map-reduce로 교체 + map 단계 메서드 추가 |
| `server/tests/test_templated_summary_service.py` | `test_summarize_truncates_long_input` 제거, map-reduce 테스트 추가 |
| (설정) `server/settings.py` | **신규 설정 없음** — 기존 `summary_text_chunk_chars`(분할 크기), `summary_concurrency`(병렬 한도) 재사용 |

라우트/리포지토리/스키마/PDF 렌더/마이그레이션은 **무수정**. `summarize_for_template` 시그니처도 그대로라 `routes/audio.py` 호출부 영향 없음.

## 설계 (map-reduce)

`summarize_for_template` 흐름:
```
1. 빈 텍스트 → 422                                    (그대로)
2. len(text) <= summary_pdf_max_input_chars(48000)
     → 짧음: _create_structured_summary(text, ...) 단일 호출  (그대로, 기존 경로)
   else
     → 김: map-reduce
        a. _split_text(text, window=summary_text_chunk_chars=16000)
             → 윈도우 목록. SummaryService._split_text 패턴 차용(줄/공백 경계 우선, 문장 중간 절단 최소화)
        b. asyncio.Semaphore(summary_concurrency) + gather 로 각 윈도우 병렬 처리
             → _summarize_window(window, template): 템플릿 섹션 관점으로 "사실 보존" 부분요약(prose/bullets)
             → 결과 = [부분요약1, 부분요약2, ...]  (메모리 리스트, 저장 안 함)
        c. 부분요약들을 이어붙여 _create_structured_summary(combined, template, ...) 로 reduce
             → 최종 섹션 JSON
3. _normalize_payload(...)                            (그대로)
```

### map 단계(`_summarize_window`)의 핵심
- 일반 요약이 아니라 **템플릿-인지 부분요약**: 프롬프트에 템플릿 섹션 label/description을 전달해
  "이 구간에서 각 섹션에 해당하는 사실을 빠짐없이 보존하라"고 지시.
  → 회의록의 `decisions`/`action_items` 같은 항목이 일반 요약 과정에서 누락되는 것을 방지.
- 출력은 자유 텍스트(불릿 허용). 구조화(JSON)는 reduce 단계에 **한 곳에서만** 수행해 일관성 유지.

### reduce 단계
- 기존 `_create_structured_summary`를 **그대로 재사용**(입력 텍스트만 원문 대신 "부분요약 묶음"). 추가 변경 없음.

### 재사용할 기존 코드
- 분할/병렬 패턴: `server/services/summary_service.py`의 `_split_text`, `_summarize_chunks`(`asyncio.Semaphore` + `create_task` + `gather`, 실패 시 잔여 태스크 cancel) 구조를 그대로 본떠 작성.
- 에러 매핑: 기존 `_create_structured_summary`의 `APIError→502 / 기타→500 / 빈 응답·invalid JSON→502` 패턴 동일 적용.

### 실패 정책 (과한 방어 금지 — README 규칙)
- map 윈도우 중 하나라도 실패하면 기존 에러 매핑대로 전파(잔여 태스크 취소 후 502/500). 부분 성공 합성은 범위 외.

### 알려진 한계 (정직하게 명시)
- map 후 "부분요약 묶음"이 다시 상한(48000자)을 넘는 극단적 길이(수 시간+)는 현재 1단계 reduce로는 또 초과할 수 있음.
  → 1차 범위에서는 **부분요약 묶음이 상한을 넘으면 한 번 더 분할 요약(재귀 1회)** 으로 안전하게 줄인 뒤 reduce.
    (truncation으로 되돌아가지 않는다.) 구현은 단순 루프로 처리.

## 비용/성능 비교
- 짧음: LLM 1회 (변화 없음).
- 김: map N회(병렬) + reduce 1회 = **N+1회**. truncation 대비 **내용 손실 0**, full_text 단일 호출 대비 토큰은 더 들지만 컨텍스트 초과를 구조적으로 회피하고 병렬이라 체감 지연을 억제.

## 테스트 (`uv run pytest tests/test_templated_summary_service.py`, Fake OpenAI client 재사용)
- **제거**: `test_summarize_truncates_long_input`(truncation 전제).
- **추가**:
  - 짧은 입력 → LLM 1회만 호출(기존 단일 경로 유지) 검증.
  - 긴 입력 → map 호출이 윈도우 수만큼 + reduce 1회 발생(핸들러가 프롬프트 종류로 map/reduce 구분 카운트).
  - 긴 입력의 **전체 내용이 map에 빠짐없이 전달**됨(마커 문자 총 개수가 보존 = 잘림 없음) 검증.
  - 동시 실행 수가 `summary_concurrency` 이하 유지(SummaryService 동시성 테스트 방식 차용).
  - reduce 결과가 템플릿 섹션 key로 정규화(`_normalize_payload`) 검증.
  - (선택) 부분요약 묶음이 상한 초과 시 재귀 1회 동작 검증.

## 검증
1. `uv run pytest tests/test_templated_summary_service.py` — 전부 통과.
2. `uv run pytest` — 기존 무회귀(이번 작업과 무관한 사전 실패 5건 제외).
3. (선택, 실제 OpenAI) 긴 샘플 텍스트 1건으로 `summarize_for_template` → `SummaryPdfService.render` 스모크:
   뒷부분 내용이 PDF 섹션에 반영되는지(= truncation 회귀 없음) 눈으로 확인.
