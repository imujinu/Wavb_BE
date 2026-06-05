# 내 작업 드래그/다중선택 백엔드 플랜 문서 생성

## 요약

- 새 문서 `plan/work-items-drag-multiselect.md`를 생성한다.
- 문서에는 실제 폴더 생성, 드래그앤드롭 정렬, 파일끼리 겹쳐 폴더 생성, 다중선택 파일 이동을 위한 백엔드 설계를 담는다.
- v1 범위는 1-depth 폴더, list 기반 API, 다중선택 이동, 파일 겹침 폴더 생성, 루트/폴더 내부 정렬 저장까지로 잡는다.
- 중첩 폴더, 폴더 병합, 실시간 동기화, AI 폴더명 추천은 v1에서 제외한다.

## 문서 구성

- 제목: `내 작업 폴더/드래그앤드롭/다중선택 v1 백엔드 플랜`
- 포함할 섹션:
  - `Summary`
  - `V1 Scope`
  - `Out of Scope`
  - `DB Migration`
  - `API Design`
  - `Service Design`
  - `Repository Design`
  - `Transaction Rules`
  - `Error Policy`
  - `Frontend Contract Notes`
  - `Test Plan`

## 핵심 내용

- 새 테이블 `folders` 추가
  - `id`
  - `user_id`
  - `name`
  - `sort_order`
  - `created_at`
  - `updated_at`

- 기존 `transcripts` 테이블에 컬럼 추가
  - `folder_id`
  - `sort_order`

- 주요 API 설계
  - `POST /folders`: 폴더 생성
  - `PATCH /folders/{folder_id}`: 폴더명 수정
  - `DELETE /folders/{folder_id}`: 폴더 삭제
  - `GET /work-items`: 루트의 폴더 + 파일 통합 목록 조회
  - `GET /folders/{folder_id}/items`: 폴더 내부 파일 목록 조회
  - `PATCH /files/folder`: 파일 단건/다중 이동
  - `POST /folders/from-files`: 파일끼리 겹쳐 새 폴더 생성
  - `PATCH /work-items/reorder`: 루트/폴더 내부 순서 저장

- list 기반 설계 강조
  - 파일 이동은 `transcript_ids: []`를 사용한다.
  - 정렬 저장은 `items: []`를 사용한다.
  - 단건 이동 API를 따로 만들지 않고, 단건도 배열 길이 1로 처리한다.

## v1 기본 정책

- 폴더는 1-depth만 지원한다.
- 루트 파일끼리 겹쳤을 때만 새 폴더 생성을 허용한다.
- 파일을 기존 폴더 위에 드롭하면 해당 폴더로 이동한다.
- 다중선택한 파일도 같은 `PATCH /files/folder` API로 이동한다.
- 루트에서는 폴더와 파일을 함께 정렬할 수 있다.
- 폴더 내부에서는 파일만 정렬할 수 있다.
- 검색 중에는 프론트에서 드래그 정렬을 비활성화한다.
- 모든 변경은 인증 사용자 소유 데이터에만 적용한다.
- 폴더 삭제 시 내부 파일은 삭제하지 않고 루트로 이동한다.

## 검증 기준

- 문서 생성 후 `plan/work-items-drag-multiselect.md`가 존재해야 한다.
- 문서에 아래 API가 반드시 포함되어야 한다.
  - `PATCH /files/folder`
  - `POST /folders/from-files`
  - `PATCH /work-items/reorder`
  - `GET /work-items`
- 이번 작업은 계획 문서 생성만 수행하고, 프론트 소스 코드는 수정하지 않는다.

## 가정

- 백엔드는 인증 사용자 ID를 모든 폴더/파일 작업의 기준으로 사용한다.
- 기존 `GET /files`는 유지할 수 있지만, 내 작업 화면의 메인 조회는 `GET /work-items`로 전환한다.
- v1에서는 복잡한 충돌 해결 대신 마지막 저장 우선 정책을 사용할 수 있다.
- reorder, 다중 이동, 파일 겹침 폴더 생성은 모두 트랜잭션으로 처리한다.
