from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from dependencies.auth import get_auth_service
from schemas.auth import TokenResponse, UserCreate, UserLogin
from services.auth.auth_service import AuthService


class RefreshRequest(BaseModel):
    refresh_token: str

router = APIRouter(prefix="/auth", tags=["auth"])


# 회원가입 성공 시 생성된 사용자 UUID만 반환하는 응답 모델
class RegisterResponse(BaseModel):
    user_id: str


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="이메일과 비밀번호로 새 사용자를 가입시킨다.",
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
    summary="이메일과 비밀번호를 검증하고 JWT 토큰을 발급한다.",
)
async def login(
    body: UserLogin,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
   
    # 1. 서비스 레이어에 로그인 위임
    # 2. 발급된 TokenResponse를 그대로 클라이언트에 반환
    return await auth_service.login(email=body.email, password=body.password)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="refresh token을 검증하고 새 JWT 토큰 쌍을 발급한다.",
)
def refresh(
    body: RefreshRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    # 1. refresh_token 검증 후 새 access_token + refresh_token 쌍 발급 (rotation)
    return auth_service.refresh_tokens(body.refresh_token)
