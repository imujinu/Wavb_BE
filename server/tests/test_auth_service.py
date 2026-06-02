"""
AuthService 단위 테스트.

FakeAuthRepository를 통해 DB 없이 register/login/decode_access_token 동작을 검증한다.
"""
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from services.auth_service import AuthService


# --- Fake Repository ---

class FakeAuthRepository:
    """
    테스트용 AuthRepository 대역(Fake).
    실제 DB 접근 없이 인메모리 dict로 사용자 데이터를 관리한다.
    """

    def __init__(self) -> None:
        # 이메일 → 사용자 dict 매핑 저장소
        self._store: dict[str, dict] = {}

    async def create_user(self, nickname: str, email: str, password_hash: str) -> UUID:
        user_id = uuid4()
        self._store[email] = {
            "id": user_id,
            "nickname": nickname,
            "email": email,
            "password_hash": password_hash,
        }
        return user_id

    async def get_user_by_email(self, email: str) -> dict | None:
        return self._store.get(email)


def make_service() -> AuthService:
    """테스트용 AuthService 인스턴스를 생성한다."""
    service = AuthService.__new__(AuthService)
    service._repository = FakeAuthRepository()
    from settings import get_settings
    service._settings = get_settings()
    return service


# --- register 테스트 ---

@pytest.mark.asyncio
async def test_register_returns_uuid() -> None:
    # 정상 회원가입 시 UUID를 반환하는지 검증
    service = make_service()
    user_id = await service.register("홍길동", "hong@example.com", "pass1234")
    assert isinstance(user_id, UUID)


@pytest.mark.asyncio
async def test_register_duplicate_email_raises_409() -> None:
    # 동일 이메일로 두 번 등록 시 HTTP 409가 발생하는지 검증
    service = make_service()
    await service.register("홍길동", "hong@example.com", "pass1234")

    with pytest.raises(HTTPException) as exc_info:
        await service.register("이름다름", "hong@example.com", "other1234")

    assert exc_info.value.status_code == 409


# --- login 테스트 ---

@pytest.mark.asyncio
async def test_login_returns_token_response() -> None:
    # 올바른 자격증명으로 로그인 시 access/refresh 토큰을 반환하는지 검증
    service = make_service()
    await service.register("홍길동", "hong@example.com", "pass1234")

    token_response = await service.login("hong@example.com", "pass1234")

    assert token_response.access_token
    assert token_response.refresh_token
    assert token_response.token_type == "bearer"


@pytest.mark.asyncio
async def test_login_wrong_email_raises_401() -> None:
    # 존재하지 않는 이메일로 로그인 시 HTTP 401이 발생하는지 검증
    service = make_service()

    with pytest.raises(HTTPException) as exc_info:
        await service.login("nobody@example.com", "pass1234")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_login_wrong_password_raises_401() -> None:
    # 잘못된 비밀번호로 로그인 시 HTTP 401이 발생하는지 검증
    service = make_service()
    await service.register("홍길동", "hong@example.com", "pass1234")

    with pytest.raises(HTTPException) as exc_info:
        await service.login("hong@example.com", "wrongpassword")

    assert exc_info.value.status_code == 401


# --- decode_access_token 테스트 ---

@pytest.mark.asyncio
async def test_decode_access_token_returns_current_user() -> None:
    # 발급된 access_token을 디코딩하면 올바른 CurrentUser를 반환하는지 검증
    from utils import jwt_utils
    from settings import get_settings

    service = make_service()
    await service.register("홍길동", "hong@example.com", "pass1234")
    token_response = await service.login("hong@example.com", "pass1234")

    current_user = jwt_utils.decode_access_token(token_response.access_token, get_settings())

    assert current_user.email == "hong@example.com"
    assert isinstance(current_user.user_id, UUID)


def test_decode_access_token_with_refresh_token_raises_401() -> None:
    # refresh_token을 access_token 자리에 사용하면 HTTP 401이 발생하는지 검증
    import jwt
    from utils import jwt_utils
    from settings import get_settings
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    settings = get_settings()
    secret = settings.jwt_secret_key or "test-secret"
    now = datetime.now(tz=timezone.utc)
    # refresh 타입 토큰 직접 생성
    refresh_token = jwt.encode(
        {
            "sub": str(uuid4()),
            "email": "hong@example.com",
            "type": "refresh",
            "exp": now + timedelta(days=30),
        },
        secret,
        algorithm=settings.jwt_algorithm,
    )

    # decode_access_token이 사용할 설정 객체 준비 (시크릿 키 일치 보장)
    mock_settings = MagicMock()
    mock_settings.jwt_secret_key = secret
    mock_settings.jwt_algorithm = settings.jwt_algorithm

    with pytest.raises(HTTPException) as exc_info:
        jwt_utils.decode_access_token(refresh_token, mock_settings)

    assert exc_info.value.status_code == 401



def test_decode_access_token_invalid_token_raises_401() -> None:
    # 완전히 잘못된 토큰 문자열에 대해 HTTP 401이 발생하는지 검증
    from unittest.mock import MagicMock
    from utils import jwt_utils

    mock_settings = MagicMock()
    mock_settings.jwt_secret_key = "some-secret"
    mock_settings.jwt_algorithm = "HS256"

    with pytest.raises(HTTPException) as exc_info:
        jwt_utils.decode_access_token("not.a.valid.token", mock_settings)

    assert exc_info.value.status_code == 401
