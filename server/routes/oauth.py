"""OAuth 소셜 로그인 라우트"""
from fastapi import APIRouter, Depends

from dependencies.auth import get_oauth_service
from schemas.auth import OAuthLoginRequest, OAuthNaverLoginRequest, TokenResponse
from services.auth.oauth_service import OAuthService

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


@router.post("/google", response_model=TokenResponse)
async def google_login(
    request: OAuthLoginRequest,
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> TokenResponse:
    # 구글 인가 코드로 로그인
    return await oauth_service.login_with_google(code=request.code)


@router.post("/kakao", response_model=TokenResponse)
async def kakao_login(
    request: OAuthLoginRequest,
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> TokenResponse:
    # 카카오 인가 코드로 로그인
    return await oauth_service.login_with_kakao(code=request.code)


@router.post("/naver", response_model=TokenResponse)
async def naver_login(
    request: OAuthNaverLoginRequest,
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> TokenResponse:
    # 네이버 인가 코드 + state 로 로그인
    return await oauth_service.login_with_naver(code=request.code, state=request.state)
