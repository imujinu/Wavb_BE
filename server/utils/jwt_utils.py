from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from fastapi import HTTPException

from schemas.auth import CurrentUser, TokenResponse
from settings import Settings


def create_token_pair(user_id: UUID, email: str, settings: Settings) -> TokenResponse:
    """
    기능 요약: user_id와 email을 담은 access/refresh JWT 토큰 쌍을 생성하여 반환한다.

    기능 흐름:
        1. 현재 UTC 시각을 기준으로 access_token 생성 (단기 만료)
        2. 현재 UTC 시각을 기준으로 refresh_token 생성 (장기 만료)
        3. TokenResponse에 묶어 반환

    파라미터:
        user_id: 내부 사용자 UUID
        email: 사용자 이메일
        settings: JWT 설정값 (secret_key, algorithm, 만료 시간)
    """
    now = datetime.now(tz=timezone.utc)

    # 1. access_token 생성 — 단기 만료 토큰으로 API 접근 권한 부여
    access_token = jwt.encode(
        {
            "sub": str(user_id),
            "email": email,
            "type": "access",
            "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    # 2. refresh_token 생성 — 장기 만료 토큰으로 access_token 갱신에 사용
    refresh_token = jwt.encode(
        {
            "sub": str(user_id),
            "email": email,
            "type": "refresh",
            "exp": now + timedelta(days=settings.jwt_refresh_token_expire_days),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    # 3. 두 토큰을 묶어 반환
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
    )


def decode_access_token(token: str, settings: Settings) -> CurrentUser:
    """
    기능 요약: access JWT 토큰을 검증하고, 담긴 사용자 정보를 CurrentUser로 반환한다.

    기능 흐름:
        1. jwt.decode()로 서명 및 만료 검증
        2. 만료 시 401 예외 반환
        3. 서명 오류 등 기타 JWT 오류 시 401 예외 반환
        4. 토큰 타입이 "access"가 아닐 경우 401 예외 반환
        5. 검증 완료 후 CurrentUser 반환

    파라미터:
        token: 클라이언트로부터 전달받은 JWT access 토큰 문자열
        settings: JWT 검증에 필요한 설정값 (secret_key, algorithm)
    """
    try:
        # 1. 서명 검증 및 만료 시간 확인
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="토큰이 만료되었습니다.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    # 2. access 타입 토큰인지 확인 — refresh 토큰으로 API 접근 시도 차단
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="access 토큰이 아닙니다.")

    # 3. 검증된 payload로 CurrentUser 컨텍스트 생성
    return CurrentUser(
        user_id=UUID(payload["sub"]),
        email=payload["email"],
    )


def decode_refresh_token(token: str, settings: Settings) -> dict:
    """
    기능 요약: refresh JWT 토큰을 검증하고, 담긴 클레임 dict를 반환한다.

    기능 흐름:
        1. jwt.decode()로 서명 및 만료 검증
        2. 만료 시 401 예외 반환
        3. 서명 오류 등 기타 JWT 오류 시 401 예외 반환
        4. 토큰 타입이 "refresh"가 아닐 경우 401 예외 반환
        5. 검증 완료 후 payload dict 반환

    파라미터:
        token: 클라이언트로부터 전달받은 JWT refresh 토큰 문자열
        settings: JWT 검증에 필요한 설정값 (secret_key, algorithm)
    """
    try:
        # 1. 서명 검증 및 만료 시간 확인
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="refresh 토큰이 만료되었습니다.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="유효하지 않은 refresh 토큰입니다.")

    # 2. refresh 타입 토큰인지 확인
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="refresh 토큰이 아닙니다.")

    # 3. 검증된 payload 반환
    return payload
