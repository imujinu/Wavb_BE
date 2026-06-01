# OAuth 소셜 로그인 구현 플랜 (Google / Kakao / Naver)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 모바일 앱이 각 OAuth 제공자 SDK에서 발급받은 인가 코드를 백엔드로 전달하면, 백엔드가 토큰 교환 → 사용자 조회/생성 → JWT 발급까지 처리하는 소셜 로그인 API를 구현한다.

**Architecture:**
- 모바일 앱 → 코드 전송 → `POST /auth/oauth/{google|kakao|naver}`
- 백엔드: httpx로 제공자 API 호출 → `user_oauth_accounts` 테이블에서 계정 찾기/생성 → 기존 AuthService와 동일한 JWT 반환
- 새 파일: `OAuthRepository`, `OAuthService`, `routes/oauth.py` (기존 auth 레이어를 최대한 재활용)

**Tech Stack:** FastAPI, asyncpg, PyJWT, httpx

---

## Task 1: DB 마이그레이션

**왜:** OAuth 사용자는 비밀번호가 없고, 제공자별 고유 ID로 계정을 연결해야 하므로 별도 테이블이 필요하다. 기존 `users.password_hash NOT NULL` 제약도 완화해야 OAuth 사용자를 저장할 수 있다.

**Files:**
- Create: `server/db/migrations/006_add_oauth_accounts.sql`

- [ ] **마이그레이션 파일 작성 후 DB 적용**

```sql
-- OAuth 사용자는 password 없으므로 NULL 허용
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;

CREATE TABLE IF NOT EXISTS user_oauth_accounts (
    id            UUID        PRIMARY KEY,
    user_id       UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT        NOT NULL,   -- 'google' | 'kakao' | 'naver'
    oauth_id      TEXT        NOT NULL,   -- 제공자가 부여한 고유 사용자 ID
    provider_data JSONB,                  -- 제공자 응답 원본 보관
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_provider_oauth_id UNIQUE (provider, oauth_id)
);

CREATE INDEX IF NOT EXISTS idx_oauth_accounts_user_id
    ON user_oauth_accounts(user_id);
```

```bash
docker exec -i $(docker-compose ps -q db) psql -U postgres -d recordoc \
  < server/db/migrations/006_add_oauth_accounts.sql
```

```bash
git add server/db/migrations/006_add_oauth_accounts.sql
git commit -m "feat: add user_oauth_accounts table and allow nullable password_hash"
```

---

## Task 2: 패키지 의존성 + Settings + Schemas

**왜:** httpx는 제공자 API 호출에 필요한데 현재 dev 의존성에만 있어 프로덕션에서 사용 불가하다. Settings에는 각 제공자의 Client ID/Secret/Redirect URI가 없으면 토큰 교환 요청 자체가 불가하다.

**Files:**
- Modify: `server/pyproject.toml` — httpx를 main 의존성으로 이동
- Modify: `server/settings.py`
- Modify: `server/.env.example`
- Modify: `server/schemas/auth.py`

- [ ] **pyproject.toml: httpx를 `[project].dependencies`로 이동, dev에서 제거 후 `uv sync`**

- [ ] **settings.py: 기존 JWT 필드 아래에 추가**

```python
# Google OAuth
google_client_id: str = Field("", alias="GOOGLE_CLIENT_ID")
google_client_secret: str = Field("", alias="GOOGLE_CLIENT_SECRET")
google_redirect_uri: str = Field("", alias="GOOGLE_REDIRECT_URI")

# Kakao OAuth (REST API 키)
kakao_client_id: str = Field("", alias="KAKAO_CLIENT_ID")
kakao_client_secret: str = Field("", alias="KAKAO_CLIENT_SECRET")
kakao_redirect_uri: str = Field("", alias="KAKAO_REDIRECT_URI")

# Naver OAuth
naver_client_id: str = Field("", alias="NAVER_CLIENT_ID")
naver_client_secret: str = Field("", alias="NAVER_CLIENT_SECRET")
naver_redirect_uri: str = Field("", alias="NAVER_REDIRECT_URI")
```

- [ ] **.env.example에 위 항목 빈 값으로 추가**

- [ ] **schemas/auth.py: 기존 스키마 하단에 추가**

```python
class OAuthLoginRequest(BaseModel):
    """Google / Kakao OAuth 인가 코드 로그인 요청"""
    model_config = ConfigDict(frozen=True)
    code: str

class OAuthNaverLoginRequest(BaseModel):
    """Naver OAuth 인가 코드 로그인 요청 — state는 CSRF 방어용"""
    model_config = ConfigDict(frozen=True)
    code: str
    state: str
```

```bash
git add server/pyproject.toml server/settings.py server/.env.example server/schemas/auth.py
git commit -m "feat: add OAuth provider settings, schemas, and move httpx to main deps"
```

---

## Task 3: OAuthRepository

**왜:** 기존 `AuthRepository`는 이메일/비밀번호 기반이라 provider+oauth_id 기반 조회가 없다. 관심사를 분리하기 위해 OAuth 전용 레포지토리를 별도로 만든다.

**Files:**
- Create: `server/repositories/oauth_repository.py`

- [ ] **구현**

```python
"""OAuth 계정 DB 조회 및 생성"""
import json
from uuid import UUID, uuid4
from db.connection import DatabaseConnection


class OAuthRepository:
    def __init__(self, connection: DatabaseConnection):
        self._connection = connection

    async def get_user_by_oauth(self, provider: str, oauth_id: str) -> dict | None:
        # provider + oauth_id 조합으로 users 테이블과 JOIN 조회
        row = await self._connection.fetchrow(
            """
            SELECT u.id, u.email, u.nickname
            FROM users u
            INNER JOIN user_oauth_accounts oa ON u.id = oa.user_id
            WHERE oa.provider = $1 AND oa.oauth_id = $2
            """,
            provider, oauth_id,
        )
        return dict(row) if row else None

    async def create_user_with_oauth(
        self, email: str, nickname: str, provider: str, oauth_id: str, provider_data: dict
    ) -> UUID:
        # users 먼저 생성 (password_hash 없음), 이후 oauth 계정 연동
        user_id = uuid4()
        await self._connection.execute(
            "INSERT INTO users (id, nickname, email) VALUES ($1, $2, $3)",
            user_id, nickname, email,
        )
        await self._connection.execute(
            """
            INSERT INTO user_oauth_accounts (id, user_id, provider, oauth_id, provider_data)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            uuid4(), user_id, provider, oauth_id, json.dumps(provider_data),
        )
        return user_id
```

```bash
git add server/repositories/oauth_repository.py
git commit -m "feat: add OAuthRepository for provider-based user lookup and creation"
```

---

## Task 4: OAuthService

**왜:** 각 제공자마다 토큰 교환 URL·파라미터·사용자 정보 필드가 다르다. 이 차이를 서비스 레이어에서 흡수하고, 상위 레이어(라우트)에는 `TokenResponse`만 돌려주면 된다. JWT 발급은 기존 AuthService와 동일한 로직을 그대로 사용한다.

**Files:**
- Create: `server/services/oauth_service.py`

- [ ] **구현**

```python
"""OAuth 제공자별 인가 코드 교환 및 JWT 발급 서비스"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import jwt
from fastapi import HTTPException

from repositories.oauth_repository import OAuthRepository
from schemas.auth import TokenResponse
from settings import Settings, get_settings


class OAuthService:
    def __init__(self, repository: OAuthRepository, settings: Settings | None = None):
        self._repository = repository
        self._settings = settings or get_settings()

    async def login_with_google(self, code: str) -> TokenResponse:
        async with httpx.AsyncClient() as client:
            # 1. 인가 코드 → Google 액세스 토큰
            token_resp = await self._post(
                client, "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": self._settings.google_client_id,
                    "client_secret": self._settings.google_client_secret,
                    "redirect_uri": self._settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
                error_label="Google 토큰 교환",
            )
            # 2. 액세스 토큰 → 사용자 정보
            info = await self._get(
                client, "https://www.googleapis.com/oauth2/v3/userinfo",
                token=token_resp["access_token"],
                error_label="Google 사용자 정보",
            )
        user_id = await self._find_or_create(
            provider="google",
            oauth_id=info["sub"],
            email=info.get("email", ""),
            nickname=info.get("name", info.get("email", "").split("@")[0]),
            provider_data=info,
        )
        return self._issue_tokens(user_id, info.get("email", ""))

    async def login_with_kakao(self, code: str) -> TokenResponse:
        async with httpx.AsyncClient() as client:
            # 1. 인가 코드 → Kakao 액세스 토큰
            token_resp = await self._post(
                client, "https://kauth.kakao.com/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._settings.kakao_client_id,
                    "client_secret": self._settings.kakao_client_secret,
                    "redirect_uri": self._settings.kakao_redirect_uri,
                    "code": code,
                },
                error_label="Kakao 토큰 교환",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            # 2. 액세스 토큰 → 사용자 정보 (POST)
            info = await self._post(
                client, "https://kapi.kakao.com/v2/user/me",
                data={},
                error_label="Kakao 사용자 정보",
                headers={"Authorization": f"Bearer {token_resp['access_token']}"},
            )
        kakao_account = info.get("kakao_account", {})
        oauth_id = str(info["id"])
        email = kakao_account.get("email", f"{oauth_id}@kakao.local")
        nickname = kakao_account.get("profile", {}).get("nickname", f"kakao_{oauth_id}")
        user_id = await self._find_or_create("kakao", oauth_id, email, nickname, info)
        return self._issue_tokens(user_id, email)

    async def login_with_naver(self, code: str, state: str) -> TokenResponse:
        async with httpx.AsyncClient() as client:
            # 1. 인가 코드 + state → Naver 액세스 토큰
            token_resp = await self._post(
                client, "https://nid.naver.com/oauth2.0/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._settings.naver_client_id,
                    "client_secret": self._settings.naver_client_secret,
                    "redirect_uri": self._settings.naver_redirect_uri,
                    "code": code,
                    "state": state,
                },
                error_label="Naver 토큰 교환",
            )
            # 2. 액세스 토큰 → 사용자 정보
            info = await self._get(
                client, "https://openapi.naver.com/v1/nid/me",
                token=token_resp["access_token"],
                error_label="Naver 사용자 정보",
            )
        naver_resp = info.get("response", {})
        oauth_id = naver_resp["id"]
        email = naver_resp.get("email", f"{oauth_id}@naver.local")
        nickname = naver_resp.get("name", f"naver_{oauth_id}")
        user_id = await self._find_or_create("naver", oauth_id, email, nickname, naver_resp)
        return self._issue_tokens(user_id, email)

    # ── HTTP 헬퍼 ──────────────────────────────────────────────

    async def _post(
        self, client: httpx.AsyncClient, url: str, *, data: dict,
        error_label: str, headers: dict | None = None,
    ) -> dict:
        try:
            resp = await client.post(url, data=data, headers=headers or {})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"{error_label} 실패: {e.response.status_code}")

    async def _get(
        self, client: httpx.AsyncClient, url: str, *, token: str, error_label: str
    ) -> dict:
        try:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"{error_label} 실패: {e.response.status_code}")

    # ── 공통 헬퍼 ─────────────────────────────────────────────

    async def _find_or_create(
        self, provider: str, oauth_id: str, email: str, nickname: str, provider_data: dict
    ) -> UUID:
        existing = await self._repository.get_user_by_oauth(provider, oauth_id)
        if existing:
            return existing["id"]
        return await self._repository.create_user_with_oauth(
            email=email, nickname=nickname,
            provider=provider, oauth_id=oauth_id, provider_data=provider_data,
        )

    def _issue_tokens(self, user_id: UUID, email: str) -> TokenResponse:
        # AuthService와 동일한 JWT 발급 로직 (sub, email, type, exp)
        now = datetime.now(tz=timezone.utc)
        access_token = jwt.encode(
            {"sub": str(user_id), "email": email, "type": "access",
             "exp": now + timedelta(minutes=self._settings.jwt_access_token_expire_minutes)},
            self._settings.jwt_secret_key, algorithm=self._settings.jwt_algorithm,
        )
        refresh_token = jwt.encode(
            {"sub": str(user_id), "email": email, "type": "refresh",
             "exp": now + timedelta(days=self._settings.jwt_refresh_token_expire_days)},
            self._settings.jwt_secret_key, algorithm=self._settings.jwt_algorithm,
        )
        return TokenResponse(access_token=access_token, refresh_token=refresh_token)
```

- [ ] **OAuthService 유닛 테스트 작성 및 통과 확인**

`server/tests/test_oauth_service.py` 에 FakeOAuthRepository + httpx mock으로 Google/Kakao/Naver 각 1개 케이스, 502 에러 케이스 1개 총 4개 테스트 작성.

```bash
cd server && uv run pytest tests/test_oauth_service.py -v
```

```bash
git add server/services/oauth_service.py server/tests/test_oauth_service.py
git commit -m "feat: add OAuthService with Google/Kakao/Naver token exchange and JWT issuance"
```

---

## Task 5: Routes + Dependencies + main.py 등록

**왜:** 서비스 로직이 완성됐어도 FastAPI 라우터에 등록하고 DI(의존성 주입)를 연결해야 외부에서 API를 호출할 수 있다. 기존 `get_auth_service` 패턴을 그대로 따라 `get_oauth_service`를 만든다.

**Files:**
- Create: `server/routes/oauth.py`
- Modify: `server/dependencies/auth.py`
- Modify: `server/main.py`

- [ ] **routes/oauth.py 작성**

```python
"""OAuth 소셜 로그인 라우트"""
from fastapi import APIRouter, Depends
from dependencies.auth import get_oauth_service
from schemas.auth import OAuthLoginRequest, OAuthNaverLoginRequest, TokenResponse
from services.oauth_service import OAuthService

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


@router.post("/google", response_model=TokenResponse)
async def google_login(
    request: OAuthLoginRequest,
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> TokenResponse:
    return await oauth_service.login_with_google(code=request.code)


@router.post("/kakao", response_model=TokenResponse)
async def kakao_login(
    request: OAuthLoginRequest,
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> TokenResponse:
    return await oauth_service.login_with_kakao(code=request.code)


@router.post("/naver", response_model=TokenResponse)
async def naver_login(
    request: OAuthNaverLoginRequest,
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> TokenResponse:
    return await oauth_service.login_with_naver(code=request.code, state=request.state)
```

- [ ] **dependencies/auth.py 하단에 get_oauth_service 추가**

```python
async def get_oauth_service(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[OAuthService]:
    yield OAuthService(repository=OAuthRepository(connection))
```

(파일 상단에 `from repositories.oauth_repository import OAuthRepository`, `from services.oauth_service import OAuthService` import 추가)

- [ ] **main.py에 oauth_router 등록**

```python
from routes.oauth import router as oauth_router
# ...
app.include_router(oauth_router)
```

- [ ] **전체 테스트 실행**

```bash
cd server && uv run pytest -v
```

```bash
git add server/routes/oauth.py server/dependencies/auth.py server/main.py
git commit -m "feat: register OAuth routes and get_oauth_service dependency"
```

---

## 검증

**서버 기동 후 라우트 노출 확인:**

```bash
cd server && uv run uvicorn main:app --reload
curl http://localhost:8000/openapi.json | python -m json.tool | grep "oauth"
```

Expected: `/auth/oauth/google`, `/auth/oauth/kakao`, `/auth/oauth/naver` 포함

**실제 코드 교환 테스트 (각 제공자 Developer Console에서 인가 코드 발급 후):**

```bash
curl -X POST http://localhost:8000/auth/oauth/kakao \
  -H "Content-Type: application/json" \
  -d '{"code": "<발급받은_인가코드>"}'
```

Expected: `{"access_token": "...", "refresh_token": "...", "token_type": "bearer"}`

**발급된 JWT로 보호 엔드포인트 접근 확인:**

```bash
curl -X POST http://localhost:8000/audio/transcripts \
  -H "Authorization: Bearer <access_token>" \
  -F "file=@test.m4a"
```

Expected: 401이 아닌 정상 처리 (기존 `get_current_user` DI가 동일하게 동작)
