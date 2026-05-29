import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import bcrypt
import jwt
from fastapi import HTTPException, status

from repositories.auth_repository import AuthRepository
from schemas.auth import CurrentUser, TokenResponse
from settings import get_settings


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

        user_id = str(user["id"])
        now = datetime.now(tz=timezone.utc)

        # 3. access_token 생성 — 단기 만료 토큰으로 API 접근 권한 부여
        access_token = jwt.encode(
            {
                "sub": user_id,
                "email": email,
                "type": "access",
                "exp": now + timedelta(minutes=self._settings.jwt_access_token_expire_minutes),
            },
            self._settings.jwt_secret_key,
            algorithm=self._settings.jwt_algorithm,
        )

        # 4. refresh_token 생성 — 장기 만료 토큰으로 access_token 갱신에 사용
        refresh_token = jwt.encode(
            {
                "sub": user_id,
                "email": email,
                "type": "refresh",
                "exp": now + timedelta(days=self._settings.jwt_refresh_token_expire_days),
            },
            self._settings.jwt_secret_key,
            algorithm=self._settings.jwt_algorithm,
        )

        # 5. 두 토큰을 묶어 반환
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        )

    def decode_access_token(self, token: str) -> CurrentUser:
       
        try:
            # 1. 서명 검증 및 만료 시간 확인
            payload = jwt.decode(
                token,
                self._settings.jwt_secret_key,
                algorithms=[self._settings.jwt_algorithm],
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="토큰이 만료되었습니다.",
            )
        except jwt.InvalidTokenError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 토큰입니다.",
            )

        # 2. access 타입 토큰인지 확인 — refresh 토큰으로 API 접근 시도 차단
        if payload.get("type") != "access":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="access 토큰이 아닙니다.",
            )

        # 3. 검증된 payload로 CurrentUser 컨텍스트 생성
        return CurrentUser(
            user_id=UUID(payload["sub"]),
            email=payload["email"],
        )
