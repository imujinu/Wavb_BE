from uuid import UUID

from fastapi import HTTPException, status

from repositories.work_item_repository import WorkItemRepository
from schemas.work_items import (
    FileWorkItemResponse,
    FolderRecord,
    FolderWorkItemResponse,
    ReorderItem,
    WorkItemResponse,
)


class WorkItemService:
    def __init__(self, repository: WorkItemRepository) -> None:
        self._repository = repository

    # 기능 요약: 내 작업 루트 화면의 폴더와 파일을 하나의 목록으로 조립한다.
    # 기능 흐름: 루트 폴더와 folder_id가 없는 파일을 각각 조회한 뒤 type 필드를 붙이고 sort_order 기준으로 합친다.
    # 파라미터: user_id는 인증 사용자 UUID.
    async def list_root_items(self, user_id: UUID) -> list[WorkItemResponse]:
        folders = await self._repository.list_root_folders(user_id)
        files = await self._repository.list_root_files(user_id)
        items: list[WorkItemResponse] = [
            self._folder_response(folder) for folder in folders
        ] + [self._file_response(file) for file in files]
        return sorted(items, key=lambda item: (item.sort_order, 0 if item.type == "folder" else 1))

    # 기능 요약: 특정 폴더 내부의 파일 목록을 반환한다.
    # 기능 흐름: 폴더 소유권을 확인하고, 해당 folder_id를 가진 transcript만 조회해 file item 응답으로 변환한다.
    # 파라미터: folder_id는 열람할 폴더 UUID, user_id는 인증 사용자 UUID.
    async def list_folder_items(
        self,
        folder_id: UUID,
        user_id: UUID,
    ) -> list[FileWorkItemResponse]:
        folder = await self._repository.get_folder_by_id(folder_id, user_id)
        if folder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found.",
            )

        files = await self._repository.list_files_by_folder(folder_id, user_id)
        return [self._file_response(file) for file in files]

    async def create_folder(self, user_id: UUID, name: str) -> FolderWorkItemResponse:
        folder = await self._repository.create_folder(user_id, name)
        return self._folder_response(folder)

    async def update_folder(
        self,
        folder_id: UUID,
        user_id: UUID,
        name: str,
    ) -> FolderWorkItemResponse:
        folder = await self._repository.update_folder_name(folder_id, user_id, name)
        if folder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found.",
            )
        return self._folder_response(folder)

    async def delete_folder(self, folder_id: UUID, user_id: UUID) -> None:
        deleted = await self._repository.delete_folder(folder_id, user_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found.",
            )

    # 기능 요약: 단건 또는 다중 파일을 폴더/루트로 이동한다.
    # 기능 흐름: 대상 폴더와 모든 transcript 소유권을 확인한 뒤 transcripts.folder_id를 갱신한다.
    # 파라미터: transcript_ids는 이동 대상 파일 UUID 목록, user_id는 인증 사용자 UUID, folder_id는 대상 폴더 또는 None.
    async def move_files(
        self,
        transcript_ids: list[UUID],
        user_id: UUID,
        folder_id: UUID | None,
    ) -> None:
        unique_ids = self._unique_ids(transcript_ids)
        if folder_id is not None:
            folder = await self._repository.get_folder_by_id(folder_id, user_id)
            if folder is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Folder not found.",
                )

        owned_count = await self._repository.count_owned_transcripts(unique_ids, user_id)
        if owned_count != len(unique_ids):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found.",
            )

        await self._repository.move_files(unique_ids, user_id, folder_id)

    # 기능 요약: 루트 파일 여러 개를 새 폴더로 묶는다.
    # 기능 흐름: 모든 파일이 현재 루트에 있는지 확인한 뒤 폴더 생성과 파일 이동을 수행한다.
    # 파라미터: name은 새 폴더명, transcript_ids는 묶을 루트 파일 UUID 목록, user_id는 인증 사용자 UUID.
    async def create_folder_from_files(
        self,
        user_id: UUID,
        name: str,
        transcript_ids: list[UUID],
    ) -> FolderWorkItemResponse:
        unique_ids = self._unique_ids(transcript_ids)
        root_count = await self._repository.count_root_files(unique_ids, user_id)
        if root_count != len(unique_ids):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="All files must be root files owned by the current user.",
            )

        folder = await self._repository.create_folder_from_root_files(
            user_id=user_id,
            name=name,
            transcript_ids=unique_ids,
        )
        return self._folder_response(folder)

    # 기능 요약: 루트 또는 폴더 내부 정렬 순서를 저장한다.
    # 기능 흐름: 요청 item이 실제 컨테이너에 속하는지 검증하고 sort_order를 일괄 갱신한다.
    # 파라미터: container는 root/folder, folder_id는 폴더 컨테이너일 때 대상 폴더, items는 저장할 순서 목록.
    async def reorder_items(
        self,
        user_id: UUID,
        container: str,
        folder_id: UUID | None,
        items: list[ReorderItem],
    ) -> None:
        if container == "root":
            await self._reorder_root_items(user_id, items)
            return

        if folder_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="folder_id is required.",
            )
        await self._reorder_folder_items(user_id, folder_id, items)

    async def _reorder_root_items(self, user_id: UUID, items: list[ReorderItem]) -> None:
        folder_items = [item for item in items if item.type == "folder"]
        file_items = [item for item in items if item.type == "file"]

        if folder_items:
            folder_count = await self._repository.count_folders(
                [item.id for item in folder_items],
                user_id,
            )
            if folder_count != len(folder_items):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Some folders do not belong to the root container.",
                )

        if file_items:
            file_count = await self._repository.count_root_files(
                [item.id for item in file_items],
                user_id,
            )
            if file_count != len(file_items):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Some files do not belong to the root container.",
                )

        await self._repository.update_root_sort_orders(folder_items, file_items, user_id)

    async def _reorder_folder_items(
        self,
        user_id: UUID,
        folder_id: UUID,
        items: list[ReorderItem],
    ) -> None:
        if any(item.type != "file" for item in items):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Folder container accepts file items only.",
            )

        folder = await self._repository.get_folder_by_id(folder_id, user_id)
        if folder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found.",
            )

        file_count = await self._repository.count_files_in_folder(
            [item.id for item in items],
            folder_id,
            user_id,
        )
        if file_count != len(items):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Some files do not belong to the folder container.",
            )

        await self._repository.update_folder_file_sort_orders(items, folder_id, user_id)

    def _folder_response(self, folder: FolderRecord) -> FolderWorkItemResponse:
        return FolderWorkItemResponse(
            id=folder.id,
            name=folder.name,
            sort_order=folder.sort_order,
            created_at=folder.created_at,
            updated_at=folder.updated_at,
        )

    def _file_response(self, file) -> FileWorkItemResponse:
        return FileWorkItemResponse(
            transcript_id=file.transcript_id,
            title=file.title,
            file_uri=file.file_uri,
            original_filename=file.original_filename,
            mime_type=file.mime_type,
            status=file.status,
            sort_order=file.sort_order,
            created_at=file.created_at,
        )

    def _unique_ids(self, values: list[UUID]) -> list[UUID]:
        seen: set[UUID] = set()
        unique_values: list[UUID] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            unique_values.append(value)
        return unique_values
