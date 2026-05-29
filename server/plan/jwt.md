# JWT 인증 모듈 구현 플랜

---

## 서비스 동작 흐름 (완성 후)

### 회원가입
```
POST /auth/register { email, password }
  → 이메일 중복 체크
  → asyncio.to_thread(bcrypt.hashpw)   ← CPU bound이므로 스레드에서 실행
  → users INSERT
  → 201 { user_id }
```

### 로그인
```
POST /auth/login { email, password }
  → users SELECT WHERE email = $1
  → asyncio.to_thread(bcrypt.checkpw)
  → JWT access token + refresh token 발급
  → 200 { access_token, refresh_token, token_type: "bearer" }
```

### 인증이 필요한 API 요청
```
POST /audio/transcripts  Authorization: Bearer <token>
  → HTTPBearer() → credentials.credentials 추출
  → jwt.decode()  → 만료/서명 검증 (DB 조회 없이 payload에서 user_id, email 추출)
  → CurrentUser(user_id, email) 반환
  → ingestion_service.ingest_upload(user_id=current_user.user_id)
```

---

## Step별 구현 계획

### Step 1: 패키지 및 설정 추가
**작업:** `pyproject.toml`에 의존성 추가, `settings.py`에 JWT 설정 필드 추가

**수정 파일:**
- `server/pyproject.toml` — `"PyJWT>=2.8.0"`, `"bcrypt>=4.1.0"`, `"email-validator>=2.0"` 추가
- `server/settings.py` — 4개 필드 추가:
  ```python
  jwt_secret_key: str = Field(..., alias="JWT_SECRET_KEY")             # 필수, 기본값 없음
  jwt_algorithm: str = Field("HS256", alias="JWT_ALGORITHM")
  jwt_access_token_expire_minutes: int = Field(60, alias="JWT_ACCESS_TOKEN_EXPIRE_MINUTES")
  jwt_refresh_token_expire_days: int = Field(30, alias="JWT_REFRESH_TOKEN_EXPIRE_DAYS")
  ```
- `server/.env.example` — JWT 관련 예시 추가


**패키지 선택 이유:**
- `PyJWT` vs `python-jose`: python-jose는 유지보수가 느리고 여러 암호화 백엔드 의존성을 옵셔널로 가짐. PyJWT는 단일 패키지로 HS256 직접 지원
- `bcrypt` 직접 vs `passlib`: Python 3.12에서 passlib과 최신 bcrypt 간 deprecation warning 이슈 존재. v1에서 bcrypt 단독으로 충분

---

### Step 2: users 테이블 마이그레이션
**작업:** `003_add_users.sql` 작성

**신규 파일:** `server/db/migrations/003_add_users.sql`
```sql
CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY,
  nickname          TEXT NOT NULL,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

ALTER TABLE transcripts
  ADD CONSTRAINT fk_transcripts_user
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
```

**설계 이유:**
- `email`을 식별자로 사용: 업무용 앱은 이메일 기반 계정이 표준이고, UNIQUE 인덱스가 로그인 쿼리의 조회 키 역할
- `username`, `role` 등은 v1에서 불필요하므로 추가하지 않음
- FK `ON DELETE SET NULL`: 사용자 삭제 시 기존 transcript 보존, user_id만 NULL 처리
- **주의:** 기존 `transcripts` 데이터에 임의의 `user_id`가 있으면 FK 추가 시 실패. 마이그레이션 전 `UPDATE transcripts SET user_id = NULL` 실행 필요

**필요성:** users 테이블 없이는 회원가입/로그인 불가

---

### Step 3: Auth 스키마
**작업:** `schemas/auth.py` 신규 생성

**신규 파일:** `server/schemas/auth.py`

포함 모델:
- `UserCreate(nickname: str, email: EmailStr, password: str(min=8))` — 회원가입 요청
- `UserLogin(email: EmailStr, password: str)` — 로그인 요청
- `TokenResponse(access_token, refresh_token, token_type="bearer")` — 로그인 응답
- `CurrentUser(user_id: UUID, email: str)` — `get_current_user`가 반환하는 타입

**CurrentUser 설계 이유:** DB 조회 없이 JWT payload만으로 구성 가능. 라우터에서 `current_user.user_id`로 접근하는 단일 진입점 역할

**필요성:** 기존 `schemas/rag.py`와 분리해 인증 스키마와 RAG 스키마를 독립적으로 관리

---

### Step 4: AuthRepository
**작업:** `repositories/auth_repository.py` 신규 생성

**신규 파일:** `server/repositories/auth_repository.py`

메서드:
- `create_user(email, password_hash) → UUID`
- `get_user_by_email(email) → dict | None`

기존 `RagRepository`와 동일한 패턴 사용: `DatabaseConnection` Protocol 의존성 주입.

**필요성:** 기존 `rag_repository.py`에 users CRUD를 섞으면 단일 책임 원칙 위반

---

### Step 5: AuthService
**작업:** `services/auth_service.py` 신규 생성

**신규 파일:** `server/services/auth_service.py`

메서드:
- `register(nickname, email, password) → UUID` — 중복 이메일 체크 후 bcrypt 해싱하여 users INSERT
- `login(email, password) → TokenResponse` — 이메일 조회 → bcrypt 검증 → JWT 발급
- `decode_access_token(token) → CurrentUser` — JWT 검증 및 페이로드 파싱

**bcrypt `asyncio.to_thread` 사용 이유:** bcrypt는 CPU-bound 연산. 비동기 이벤트 루프를 블로킹하지 않으려면 별도 스레드에서 실행 필요

**`decode_access_token`에서 DB 조회 하지 않는 이유:** JWT payload에 `user_id`와 `email`을 포함하므로 매 요청마다 DB를 hit할 필요 없음. 계정 비활성화 기능이 필요할 때 추가

**필요성:** 비즈니스 로직(해싱, 토큰 발급)을 라우터에서 분리

---

### Step 6: get_current_user 의존성
**작업:** `dependencies/auth.py` 신규 생성 (`dependencies/` 디렉토리 신규)

**신규 파일:** `server/dependencies/__init__.py`, `server/dependencies/auth.py`

```python
_bearer = HTTPBearer()

async def get_auth_service(
    connection: DatabaseConnection = Depends(get_connection),
) -> AsyncIterator[AuthService]:
    yield AuthService(AuthRepository(connection))

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    auth_service: AuthService = Depends(get_auth_service),
) -> CurrentUser:
    return auth_service.decode_access_token(credentials.credentials)
```

**`HTTPBearer()` 사용 이유:** FastAPI OpenAPI UI에서 "Authorize" 버튼 자동 생성, `Authorization: Bearer <token>` 헤더 강제 파싱

**`get_auth_service` 분리 이유:** 기존 `audio.py`의 `get_rag_repository`와 동일한 패턴 유지. DB 커넥션 라이프사이클을 기존 방식과 일치

**필요성:** `get_current_user`를 Depends로 분리해야 모든 라우터에서 재사용 가능

---

### Step 7: Auth 라우터 + main.py 등록
**작업:** `routes/auth.py` 신규 생성, `main.py` 수정

**신규 파일:** `server/routes/auth.py`
- `POST /auth/register` → `AuthService.register()` → 201 `{ user_id }`
- `POST /auth/login` → `AuthService.login()` → 200 `TokenResponse`

refresh token 재발급 엔드포인트(`POST /auth/refresh`)는 v1 범위 제외. 만료 시 재로그인으로 충분

**수정 파일:** `server/main.py`
- `from routes.auth import router as auth_router` 추가
- `app.include_router(auth_router)` 추가

**필요성:** 인증 API가 없으면 토큰을 발급받을 방법이 없음

---

### Step 8: 기존 API에서 user_id 클라이언트 입력 제거
**작업:** 기존 3개 파일 수정

**수정 파일: `server/schemas/rag.py`**
- `RagChatRequest`에서 `user_id: UUID | None = None` 필드 제거
- `validate_search_scope` model_validator 제거 (user_id가 항상 토큰에서 주입되므로 검증 불필요)

**수정 파일: `server/routes/audio.py`**
- `user_id: UUID | None = Form(None)` 제거
- `current_user: CurrentUser = Depends(get_current_user)` 추가
- `ingest_upload(user_id=current_user.user_id)` 로 변경

**수정 파일: `server/routes/rag.py`**
- `user_id = current_user.user_id` 로 변경 (Request Body에서 토큰으로 출처 변경)
- `/chat`, `/chat/resume` 모두 `Depends(get_current_user)` 추가

**필요성:** 이 Step 없이는 인증 모듈이 있어도 기존 보안 취약점이 그대로 존재

---

## 파일 목록 요약

### 신규 생성 (6개)
| 파일 | 역할 |
|------|------|
| `server/db/migrations/004_add_users.sql` | users 테이블 + transcripts FK |
| `server/schemas/auth.py` | UserCreate, UserLogin, TokenResponse, CurrentUser |
| `server/repositories/auth_repository.py` | users CRUD |
| `server/services/auth_service.py` | bcrypt 해싱 + JWT 발급/검증 |
| `server/dependencies/auth.py` | get_current_user, get_auth_service |
| `server/routes/auth.py` | POST /auth/register, POST /auth/login |

### 수정 (5개)
| 파일 | 변경 내용 |
|------|-----------|
| `server/pyproject.toml` | PyJWT, bcrypt, email-validator 추가 |
| `server/settings.py` | jwt_secret_key 등 4개 필드 추가 |
| `server/main.py` | auth_router include 추가 |
| `server/schemas/rag.py` | RagChatRequest user_id 제거 |
| `server/routes/audio.py` + `rag.py` | user_id 제거, Depends(get_current_user) 추가 |

---

## 검증 방법

```bash
cd server

# 의존성 설치
uv sync

# 서버 시작 전 환경변수 확인 (.env에 JWT_SECRET_KEY 필수)
uv run uvicorn main:app --reload

# 회원가입 테스트
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "password123"}'
# → 201 { "user_id": "..." }

# 로그인 테스트
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "password123"}'
# → 200 { "access_token": "...", "refresh_token": "...", "token_type": "bearer" }

# 인증 없이 /audio/transcripts 호출 → 403
# 토큰으로 /audio/transcripts 호출 → 정상 처리

# 관련 테스트 실행
uv run pytest tests/test_rag_persistence.py tests/test_audio_routes.py -v
```

---

## 최종 산출물

구현 완료 후 `server/plan/jwt-auth.md` 파일 생성 (PLAN.md 규칙 8)
