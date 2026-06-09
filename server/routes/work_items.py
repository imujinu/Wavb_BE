from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, status

from db.connection import DatabaseConnection, get_connection
from dependencies.auth import get_current_user
from repositories.work_item_repository import WorkItemRepository
from schemas.auth import CurrentUser
from schemas.work_items import (
    CreateFolderFromFilesRequest,
    FileWorkItemResponse,
    FolderCreateRequest,
    FolderUpdateRequest,
    FolderWorkItemResponse,
    MoveFilesRequest,
    ReorderWorkItemsRequest,
    WorkItemResponse,
)
from services.work_items.work_item_service import WorkItemService


router = APIRouter(tags=["work-items"])


async def get_work_item_repository(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[WorkItemRepository]:
    yield WorkItemRepository(connection)


def get_work_item_service(
    repository: WorkItemRepository = Depends(get_work_item_repository),
) -> WorkItemService:
    return WorkItemService(repository)


@router.post(
    "/folders",
    response_model=FolderWorkItemResponse,
    summary="인증 사용자의 루트 작업공간에 새 폴더를 생성한다.",
)
async def create_folder(
    request: FolderCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    service: WorkItemService = Depends(get_work_item_service),
) -> FolderWorkItemResponse:
    """
    기능 요약: 인증 사용자의 루트에 새 폴더를 생성한다.

    기능 흐름:
        1. JWT에서 현재 사용자 id를 얻는다.
        2. WorkItemService.create_folder()로 폴더 row를 생성한다.
        3. 프론트 목록에서 바로 사용할 folder item 응답을 반환한다.

    파라미터:
        request: name 필드를 담은 폴더 생성 요청.
    """
    return await service.create_folder(current_user.user_id, request.name)


@router.patch(
    "/folders/{folder_id}",
    response_model=FolderWorkItemResponse,
    summary="사용자 소유 폴더의 이름을 수정한다.",
)
async def update_folder(
    folder_id: UUID,
    request: FolderUpdateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    service: WorkItemService = Depends(get_work_item_service),
) -> FolderWorkItemResponse:
    return await service.update_folder(folder_id, current_user.user_id, request.name)


@router.delete(
    "/folders/{folder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="사용자 소유 폴더를 삭제한다.",
)
async def delete_folder(
    folder_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    service: WorkItemService = Depends(get_work_item_service),
) -> None:
    await service.delete_folder(folder_id, current_user.user_id)


@router.get(
    "/work-items",
    response_model=list[WorkItemResponse],
    summary="인증 사용자의 루트 폴더와 파일 목록을 조회한다.",
)
async def list_work_items(
    current_user: CurrentUser = Depends(get_current_user),
    service: WorkItemService = Depends(get_work_item_service),
) -> list[WorkItemResponse]:
    return await service.list_root_items(current_user.user_id)


@router.get(
    "/folders/{folder_id}/items",
    response_model=list[FileWorkItemResponse],
    summary="지정한 폴더 안의 파일 목록을 조회한다.",
)
async def list_folder_items(
    folder_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    service: WorkItemService = Depends(get_work_item_service),
) -> list[FileWorkItemResponse]:
    return await service.list_folder_items(folder_id, current_user.user_id)


@router.patch(
    "/files/folder",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="선택한 파일들을 지정한 폴더로 이동한다.",
)
async def move_files_to_folder(
    request: MoveFilesRequest,
    current_user: CurrentUser = Depends(get_current_user),
    service: WorkItemService = Depends(get_work_item_service),
) -> None:
    await service.move_files(
        transcript_ids=request.transcript_ids,
        user_id=current_user.user_id,
        folder_id=request.folder_id,
    )


@router.post(
    "/folders/from-files",
    response_model=FolderWorkItemResponse,
    summary="선택한 파일들을 담을 새 폴더를 생성한다.",
)
async def create_folder_from_files(
    request: CreateFolderFromFilesRequest,
    current_user: CurrentUser = Depends(get_current_user),
    service: WorkItemService = Depends(get_work_item_service),
) -> FolderWorkItemResponse:
    return await service.create_folder_from_files(
        user_id=current_user.user_id,
        name=request.name,
        transcript_ids=request.transcript_ids,
    )


@router.patch(
    "/work-items/reorder",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="루트 또는 폴더 내부 작업 항목의 표시 순서를 변경한다.",
)
async def reorder_work_items(
    request: ReorderWorkItemsRequest,
    current_user: CurrentUser = Depends(get_current_user),
    service: WorkItemService = Depends(get_work_item_service),
) -> None:
    await service.reorder_items(
        user_id=current_user.user_id,
        container=request.container,
        folder_id=request.folder_id,
        items=request.items,
    )
