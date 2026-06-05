# 내 작업 폴더/드래그앤드롭/다중선택 v1 백엔드 플랜

## Summary

- 내 작업 화면에서 업로드 파일과 폴더를 함께 보여주고, 드래그앤드롭으로 정렬/이동할 수 있게 백엔드 API를 추가한다.
- v1은 1-depth 폴더, 루트 통합 목록, 폴더 내부 파일 목록, 다중선택 이동, 파일 겹침 폴더 생성, 정렬 저장을 지원한다.
- 기존 파일 엔티티는 `transcripts`를 계속 사용하고, 폴더 메타데이터만 새 `folders` 테이블로 분리한다.
- 모든 조회/변경은 인증 사용자 `user_id` 기준으로 소유권을 검증한다.

## V1 Scope

- 루트에 폴더와 파일을 함께 노출한다.
- 폴더는 1-depth만 허용하며 폴더 안에는 파일만 들어간다.
- 파일 단건 이동과 다중선택 이동은 동일한 list 기반 API로 처리한다.
- 루트 파일끼리 겹쳐 드롭하면 새 폴더를 생성하고 선택 파일들을 그 폴더로 이동한다.
- 루트에서는 폴더와 파일을 함께 정렬하고, 폴더 내부에서는 파일만 정렬한다.
- 폴더 삭제 시 내부 파일은 삭제하지 않고 루트로 이동한다.

## Out of Scope

- 중첩 폴더.
- 폴더끼리 겹쳐 병합하는 동작.
- 실시간 동기화, optimistic conflict resolution.
- AI 폴더명 추천.
- 검색 결과 상태에서의 드래그 정렬. 검색 중 정렬 비활성화는 프론트에서 처리한다.

## DB Migration

- 새 테이블 `folders` 추가:

```sql
CREATE TABLE folders (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sort_order INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_folders_user_sort
  ON folders(user_id, sort_order, created_at DESC);
```

- 기존 `transcripts` 테이블에 컬럼 추가:

```sql
ALTER TABLE transcripts
  ADD COLUMN folder_id UUID REFERENCES folders(id) ON DELETE SET NULL,
  ADD COLUMN sort_order INT NOT NULL DEFAULT 0;

CREATE INDEX idx_transcripts_user_folder_sort
  ON transcripts(user_id, folder_id, sort_order, created_at DESC);
```

- `folder_id IS NULL`인 파일은 루트 파일로 본다.
- `sort_order`는 같은 컨테이너 안에서만 의미가 있다.

## API Design

### `POST /folders`

- 폴더를 루트에 생성한다.
- 요청:

```json
{ "name": "강의자료" }
```

- 응답:

```json
{
  "id": "folder-uuid",
  "type": "folder",
  "name": "강의자료",
  "sort_order": 0,
  "created_at": "2026-06-05T12:00:00Z",
  "updated_at": "2026-06-05T12:00:00Z"
}
```

### `PATCH /folders/{folder_id}`

- 폴더명을 수정한다.
- 요청:

```json
{ "name": "수정된 이름" }
```

### `DELETE /folders/{folder_id}`

- 폴더를 삭제한다.
- 폴더 내부 파일은 삭제하지 않고 `folder_id=NULL`로 루트 이동한다.
- 삭제 후 루트 정렬은 기존 파일 `sort_order`를 유지한다.

### `GET /work-items`

- 내 작업 화면의 메인 조회 API.
- 루트의 폴더와 루트 파일을 하나의 list로 반환한다.
- 응답:

```json
[
  {
    "type": "folder",
    "id": "folder-uuid",
    "name": "강의자료",
    "sort_order": 0,
    "created_at": "2026-06-05T12:00:00Z",
    "updated_at": "2026-06-05T12:00:00Z"
  },
  {
    "type": "file",
    "transcript_id": "transcript-uuid",
    "title": "lecture",
    "file_uri": "/uploads/user-id/uuid.pdf",
    "original_filename": "lecture.pdf",
    "mime_type": "application/pdf",
    "status": "completed",
    "sort_order": 1,
    "created_at": "2026-06-05T12:00:00Z"
  }
]
```

### `GET /folders/{folder_id}/items`

- 특정 폴더 내부 파일 목록을 반환한다.
- 폴더는 1-depth이므로 응답에는 파일만 포함한다.

### `PATCH /files/folder`

- 파일 단건/다중 이동 API.
- 요청:

```json
{
  "transcript_ids": ["file-1", "file-2"],
  "folder_id": "folder-uuid"
}
```

- `folder_id=null`이면 루트로 이동한다.
- 단건 이동도 `transcript_ids` 길이 1로 처리한다.

### `POST /folders/from-files`

- 루트 파일끼리 겹쳐 새 폴더를 생성한다.
- 요청:

```json
{
  "name": "새 폴더",
  "transcript_ids": ["file-1", "file-2"]
}
```

- 모든 `transcript_ids`는 루트 파일이어야 한다.
- 폴더 생성과 파일 이동은 같은 트랜잭션에서 처리한다.

### `PATCH /work-items/reorder`

- 루트 또는 폴더 내부의 정렬 순서를 저장한다.
- 루트 정렬 요청:

```json
{
  "container": "root",
  "items": [
    { "type": "folder", "id": "folder-uuid", "sort_order": 0 },
    { "type": "file", "id": "transcript-uuid", "sort_order": 1 }
  ]
}
```

- 폴더 내부 정렬 요청:

```json
{
  "container": "folder",
  "folder_id": "folder-uuid",
  "items": [
    { "type": "file", "id": "transcript-uuid", "sort_order": 0 }
  ]
}
```

- 폴더 내부에서는 `type=file`만 허용한다.

## Service Design

- `FolderService`
  - 폴더 생성, 이름 수정, 삭제를 담당한다.
  - 삭제 시 내부 파일을 루트로 이동한 뒤 폴더를 삭제한다.

- `WorkItemService`
  - `GET /work-items`, `GET /folders/{folder_id}/items` 조회 응답을 조립한다.
  - 루트 목록에서는 folder/file item을 같은 list로 병합하고 `sort_order` 기준으로 정렬한다.

- `FileMoveService`
  - `PATCH /files/folder`와 `POST /folders/from-files`의 파일 이동을 담당한다.
  - 모든 파일 소유권과 대상 폴더 소유권을 검증한다.

- `WorkItemReorderService`
  - 루트/폴더 내부 정렬 저장을 담당한다.
  - 요청 list의 순서 또는 명시 `sort_order`를 기준으로 DB 값을 갱신한다.

## Repository Design

- `FolderRepository`
  - `create_folder(user_id, name)`
  - `update_folder_name(folder_id, user_id, name)`
  - `delete_folder(folder_id, user_id)`
  - `get_folder_by_id(folder_id, user_id)`
  - `list_root_folders(user_id)`

- `WorkItemRepository`
  - `list_root_files(user_id)`
  - `list_files_by_folder(folder_id, user_id)`
  - `move_files(transcript_ids, user_id, folder_id)`
  - `move_folder_files_to_root(folder_id, user_id)`
  - `update_file_sort_orders(items, user_id, folder_id)`
  - `update_folder_sort_orders(items, user_id)`

- 기존 `RagRepository.list_transcripts_by_user()`는 유지할 수 있지만, 내 작업 메인 화면은 `GET /work-items`로 전환한다.

## Transaction Rules

- `DELETE /folders/{folder_id}`
  - 내부 파일 루트 이동과 폴더 삭제를 하나의 트랜잭션으로 처리한다.

- `PATCH /files/folder`
  - 대상 폴더 검증과 파일 이동을 하나의 트랜잭션으로 처리한다.

- `POST /folders/from-files`
  - 폴더 생성, 파일 이동, 초기 정렬값 저장을 하나의 트랜잭션으로 처리한다.

- `PATCH /work-items/reorder`
  - 요청 list 전체의 `sort_order` 갱신을 하나의 트랜잭션으로 처리한다.
  - v1 충돌 정책은 마지막 저장 우선이다.

## Error Policy

- 인증되지 않은 요청: `401`.
- 폴더 또는 파일이 없거나 소유자가 다르면 `404`.
- 빈 `transcript_ids` 또는 빈 `items`: `422`.
- 폴더 내부에 폴더를 넣으려는 요청: `400`.
- `POST /folders/from-files`에서 루트 파일이 아닌 파일이 포함되면 `409`.
- `PATCH /work-items/reorder`에서 컨테이너에 속하지 않은 item이 포함되면 `409`.

## Frontend Contract Notes

- 검색 중에는 프론트에서 드래그 정렬과 drop action을 비활성화한다.
- 단건 이동과 다중선택 이동은 모두 `PATCH /files/folder`에 `transcript_ids` 배열로 요청한다.
- 파일을 기존 폴더 위에 드롭하면 `PATCH /files/folder`를 호출한다.
- 루트 파일끼리 겹쳐 새 폴더를 만들 때만 `POST /folders/from-files`를 호출한다.
- 정렬 저장은 drag end 이후 `PATCH /work-items/reorder`로 list 전체를 보낸다.
- 기존 `GET /files`는 단순 저장 목록 용도로 남길 수 있지만, 내 작업 화면은 `GET /work-items`를 사용한다.

## Test Plan

- DB/repository
  - `folders` migration이 테이블과 인덱스를 생성한다.
  - `transcripts.folder_id`, `transcripts.sort_order` migration이 추가된다.
  - 사용자 소유 폴더/파일만 조회 또는 변경된다.

- Folder API
  - 폴더 생성, 이름 수정, 삭제가 정상 동작한다.
  - 폴더 삭제 시 내부 파일은 루트로 이동하고 삭제되지 않는다.

- Work item 조회
  - `GET /work-items`가 루트 폴더와 루트 파일을 `sort_order` 기준으로 반환한다.
  - `GET /folders/{folder_id}/items`가 해당 폴더 내부 파일만 반환한다.

- 파일 이동/폴더 생성
  - `PATCH /files/folder`가 단건/다중 파일 이동을 모두 처리한다.
  - `POST /folders/from-files`가 루트 파일들로 새 폴더를 만들고 파일을 이동한다.
  - 타 사용자 파일 또는 폴더가 포함되면 실패한다.

- 정렬 저장
  - `PATCH /work-items/reorder`가 루트의 folder/file 정렬을 저장한다.
  - 폴더 내부 정렬 요청은 file item만 허용한다.
  - 컨테이너에 속하지 않은 item이 포함되면 실패한다.
