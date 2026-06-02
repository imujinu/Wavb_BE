import asyncio
from uuid import UUID

import bcrypt
from fastapi import HTTPException, status

from repositories.auth_repository import AuthRepository
from schemas.auth import TokenResponse
from settings import get_settings
from utils import jwt_utils


class AuthService:
    # AuthRepository를 생성자에서 주입받아 테스트 시 Fake 구현체 교체가 가능하도록 한다.
    def __init__(self, repository: AuthRepository) -> None:
        self._repository = repository
        self._settings = get_settings()

    async def register(self, nickname: str, email: str, password: str) -> UUID:
     
        # 1. 이메일 중복 확인 — 이미 존재하면 409 Conflict 반환
        existing_user = await self._repository.get_user_by_email(email)
        if existing_user is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="이미 사용 중인 이메일입니다.",
            )

        # 2. bcrypt 해싱은 CPU 집약적이므로 스레드풀로 오프로드하여 이벤트 루프 블로킹 방지
        password_hash: bytes = await asyncio.to_thread(
            bcrypt.hashpw,
            password.encode(),
            bcrypt.gensalt(),
        )

        # 3. DB에 사용자 삽입 후 생성된 UUID 반환
        user_id = await self._repository.create_user(
            nickname=nickname,
            email=email,
            password_hash=password_hash.decode(),
        )
        return user_id

    async def login(self, email: str, password: str) -> TokenResponse:
        
        # 1. 이메일로 사용자 조회 — 미존재 시 인증 실패로 401 반환 (사용자 존재 여부 노출 방지)
        user = await self._repository.get_user_by_email(email)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="이메일 또는 비밀번호가 올바르지 않습니다.",
            )

        # 2. bcrypt 비밀번호 검증 — 블로킹 연산이므로 스레드풀로 오프로드
        password_matched: bool = await asyncio.to_thread(
            bcrypt.checkpw,
            password.encode(),
            user["password_hash"].encode(),
        )
        if not password_matched:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="이메일 또는 비밀번호가 올바르지 않습니다.",
            )

        # 3. jwt_utils를 통해 access/refresh 토큰 쌍 생성 후 반환
        return jwt_utils.create_token_pair(user["id"], email, self._settings)

    def refresh_tokens(self, refresh_token: str) -> TokenResponse:

        # 1. refresh 토큰 서명·만료·타입 검증 후 payload 반환
        payload = jwt_utils.decode_refresh_token(refresh_token, self._settings)

        # 2. 새 access/refresh 토큰 쌍 발급 (rotation — 재사용 방지)
        return jwt_utils.create_token_pair(UUID(payload["sub"]), payload["email"], self._settings)

