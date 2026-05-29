from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db.connection import DatabaseConnection, get_connection
from repositories.auth_repository import AuthRepository
from schemas.auth import CurrentUser
from services.auth_service import AuthService

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
    auth_service: AuthService = Depends(get_auth_service),
) -> CurrentUser:
    
    # 1. 추출된 Bearer 토큰을 AuthService로 전달하여 검증
    return auth_service.decode_access_token(credentials.credentials)
