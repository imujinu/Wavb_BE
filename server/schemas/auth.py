from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field


# 회원가입 요청 시 클라이언트가 전송하는 입력값을 검증한다.
# email-validator 패키지가 EmailStr 검증을 담당한다.
class UserCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    nickname: str = Field(min_length=1, max_length=50)
    email: EmailStr
    password: str = Field(min_length=4)


# 로그인 요청 시 클라이언트가 전송하는 이메일/비밀번호 쌍을 담는다.
class UserLogin(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: EmailStr = "test@example.com"
    password: str = "1234"


# 로그인/토큰 갱신 성공 시 클라이언트에 반환하는 JWT 토큰 응답 모델.
# access_token 과 refresh_token 을 함께 반환해 클라이언트가 무중단 갱신을 할 수 있도록 한다.
class TokenResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# JWT 디코딩 후 라우트/서비스 계층에서 사용하는 현재 인증 사용자 컨텍스트.
# Depends(get_current_user) 패턴으로 주입된다.
class CurrentUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    email: str


class OAuthLoginRequest(BaseModel):
    """Google / Kakao OAuth 인가 코드 로그인 요청"""
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    code: str = Field(
        min_length=1,
        validation_alias=AliasChoices("code", "serverAuthCode", "server_auth_code"),
    )


class OAuthNaverLoginRequest(BaseModel):
    """Naver OAuth 인가 코드 로그인 요청 — state는 CSRF 방어용"""
    model_config = ConfigDict(frozen=True)
    code: str
    state: str
