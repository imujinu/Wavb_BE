from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from db.connection import DatabaseConnection, get_connection
from repositories.auth_repository import AuthRepository
from schemas.auth import TokenResponse, UserCreate, UserLogin
from services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


# 회원가입 성공 시 생성된 사용자 UUID만 반환하는 응답 모델
class RegisterResponse(BaseModel):
    user_id: str


async def get_auth_service(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[AuthService]:
    """
    기능 요약: 라우트 범위 AuthService를 생성하여 의존성으로 주입한다.
    audio.py의 get_rag_repository와 동일한 패턴으로, DB 커넥션을 주입받아 서비스를 구성한다.
    """
    # 1. DB 커넥션 → AuthRepository → AuthService 순으로 조립
    yield AuthService(AuthRepository(connection))


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: UserCreate,
    auth_service: AuthService = Depends(get_auth_service),
) -> RegisterResponse:
    
    # 1. 서비스 레이어에 회원가입 위임
    # 2. 생성된 UUID를 응답 모델에 담아 반환
    user_id: UUID = await auth_service.register(
        nickname=body.nickname,
        email=body.email,
        password=body.password,
    )
    return RegisterResponse(user_id=str(user_id))


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
)
async def login(
    body: UserLogin,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
   
    # 1. 서비스 레이어에 로그인 위임
    # 2. 발급된 TokenResponse를 그대로 클라이언트에 반환
    return await auth_service.login(email=body.email, password=body.password)
