from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db.connection import DatabaseConnection, get_connection
from repositories.auth_repository import AuthRepository
from schemas.auth import CurrentUser
from services.auth_service import AuthService
from settings import get_settings
from utils import jwt_utils

# Bearer 토큰 추출기 — Authorization 헤더에서 "Bearer <token>" 형식을 파싱한다.
# HTTPBearer는 헤더 미존재 시 자동으로 HTTP 403을 반환하므로 별도 검증 불필요.
_bearer = HTTPBearer()


async def get_auth_service(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[AuthService]:

    # 1. DB 커넥션을 AuthRepository에 주입
    # 2. AuthRepository를 AuthService에 주입하여 요청 범위 인스턴스 생성
    yield AuthService(AuthRepository(connection))


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> CurrentUser:

    # 1. 추출된 Bearer 토큰을 jwt_utils로 직접 검증하여 CurrentUser 반환
    return jwt_utils.decode_access_token(credentials.credentials, get_settings())


async def get_oauth_service(
    connection: DatabaseConnection = Depends(get_connection),
):
    # 순환 임포트 방지를 위해 함수 내부에서 로컬 임포트 사용
    from repositories.oauth_repository import OAuthRepository
    from services.oauth_service import OAuthService

    # 1. DB 커넥션을 OAuthRepository에 주입
    # 2. OAuthRepository를 OAuthService에 주입하여 요청 범위 인스턴스 생성
    yield OAuthService(repository=OAuthRepository(connection))
