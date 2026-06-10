from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


WorkItemKind = Literal["folder", "file"]
WorkItemContainer = Literal["root", "folder"]


class FolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name


class FolderUpdateRequest(FolderCreateRequest):
    pass


class MoveFilesRequest(BaseModel):
    transcript_ids: list[UUID] = Field(min_length=1)
    folder_id: UUID | None = None


class CreateFolderFromFilesRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    transcript_ids: list[UUID] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name


class ReorderItem(BaseModel):
    type: WorkItemKind
    id: UUID
    sort_order: int = Field(ge=0)


class ReorderWorkItemsRequest(BaseModel):
    container: WorkItemContainer
    folder_id: UUID | None = None
    items: list[ReorderItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_container(self) -> "ReorderWorkItemsRequest":
        if self.container == "root" and self.folder_id is not None:
            raise ValueError("folder_id is not allowed for root container")
        if self.container == "folder":
            if self.folder_id is None:
                raise ValueError("folder_id is required for folder container")
            if any(item.type != "file" for item in self.items):
                raise ValueError("folder container accepts file items only")
        return self


class FolderRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    sort_order: int
    created_at: Any | None = None
    updated_at: Any | None = None


class FileWorkItemRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    transcript_id: UUID
    title: str | None = None
    file_uri: str
    original_filename: str | None = None
    mime_type: str | None = None
    status: str
    content_status: str
    index_status: str
    sort_order: int
    created_at: Any | None = None


class FolderWorkItemResponse(FolderRecord):
    type: Literal["folder"] = "folder"


class FileWorkItemResponse(FileWorkItemRecord):
    type: Literal["file"] = "file"


WorkItemResponse = FolderWorkItemResponse | FileWorkItemResponse
