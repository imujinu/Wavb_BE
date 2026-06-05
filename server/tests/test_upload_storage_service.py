from uuid import UUID

import pytest
from fastapi import HTTPException

from services.files.upload_storage_service import UploadStorageService


class FakeUploadFile:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0
        self.seek_calls: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._content):
            return b""
        if size < 0:
            size = len(self._content) - self._offset
        chunk = self._content[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    async def seek(self, offset: int) -> None:
        self.seek_calls.append(offset)
        self._offset = offset


@pytest.mark.asyncio
async def test_upload_storage_saves_file_and_resets_stream(tmp_path) -> None:
    service = UploadStorageService.__new__(UploadStorageService)
    service._storage_dir = tmp_path
    service._public_path = "/uploads"
    user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    file = FakeUploadFile(b"hello")

    stored = await service.save_upload(file, "lecture.pdf", user_id)

    assert stored.uri.startswith(f"/uploads/{user_id}/")
    assert stored.uri.endswith(".pdf")
    assert stored.path.exists()
    assert stored.path.read_bytes() == b"hello"
    assert file.seek_calls == [0]


@pytest.mark.asyncio
async def test_upload_storage_rejects_empty_file(tmp_path) -> None:
    service = UploadStorageService.__new__(UploadStorageService)
    service._storage_dir = tmp_path
    service._public_path = "/uploads"

    with pytest.raises(HTTPException) as exc:
        await service.save_upload(FakeUploadFile(b""), "empty.pdf", None)

    assert exc.value.status_code == 400
