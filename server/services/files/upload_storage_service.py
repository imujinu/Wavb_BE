from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile, status

from settings import get_settings


@dataclass(frozen=True)
class StoredUpload:
    uri: str
    path: Path
    original_filename: str


class UploadStorageService:
    """
    기능 요약: 업로드 원본 파일을 서버 로컬 저장소에 보존하고 다시 열 수 있는 URI를 만든다.

    기능 흐름:
        1. 설정의 UPLOAD_STORAGE_DIR 아래에 사용자별 하위 디렉터리를 만든다.
        2. 원본 파일명 대신 UUID 기반 파일명으로 저장해 충돌과 경로 조작을 방지한다.
        3. 저장 후 UploadFile stream을 0으로 되돌려 뒤쪽 텍스트 추출/STT 단계가 다시 읽을 수 있게 한다.

    파라미터:
        file: FastAPI UploadFile
        file_name: 원본 표시 파일명 (예: lecture.pdf)
        user_id: 인증 사용자 UUID. 없으면 anonymous 디렉터리에 저장한다.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._storage_dir = Path(settings.upload_storage_dir)
        self._public_path = "/" + settings.upload_public_path.strip("/")

    async def save_upload(
        self,
        file: UploadFile,
        file_name: str,
        user_id: UUID | None,
    ) -> StoredUpload:
        suffix = Path(file_name).suffix.lower()
        stored_name = f"{uuid4()}{suffix}"
        owner_dir = self._storage_dir / (str(user_id) if user_id else "anonymous")
        owner_dir.mkdir(parents=True, exist_ok=True)
        stored_path = owner_dir / stored_name

        size = 0
        with stored_path.open("wb") as output_file:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                output_file.write(chunk)

        await file.seek(0)

        if size == 0:
            stored_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty.",
            )

        uri = f"{self._public_path}/{owner_dir.name}/{stored_name}"
        return StoredUpload(
            uri=uri,
            path=stored_path,
            original_filename=file_name,
        )

    def resolve_uri(self, uri: str) -> Path:
        """
        기능 요약: DB에 저장된 public upload URI를 서버 로컬 파일 경로로 변환한다.

        기능 흐름:
            1. URI가 설정된 public path 아래인지 검증한다.
            2. 상대 경로 조각에 경로 조작 값이 없는지 확인한다.
            3. UPLOAD_STORAGE_DIR 아래의 실제 Path를 반환한다.

        파라미터:
            uri: DB에 저장된 파일 URI (예: /uploads/user-id/file.pdf).
        """
        public_prefix = self._public_path.rstrip("/") + "/"
        if not uri.startswith(public_prefix):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported upload URI.",
            )

        relative = uri[len(public_prefix):]
        parts = Path(relative).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid upload URI.",
            )

        path = self._storage_dir.joinpath(*parts)
        storage_root = self._storage_dir.resolve()
        resolved_path = path.resolve()
        if storage_root not in resolved_path.parents and resolved_path != storage_root:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid upload URI.",
            )
        return resolved_path
