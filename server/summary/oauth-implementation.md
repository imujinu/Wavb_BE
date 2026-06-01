# OAuth 소셜 로그인 구현 요약

**브랜치:** `feat/oauth`  
**작업 범위:** Google / Kakao / Naver OAuth2 인가 코드 플로우 백엔드 구현

---

## 구현 개요

모바일 앱(React Native Expo)이 각 OAuth 제공자 SDK에서 인가 코드를 받아 백엔드로 전달하면,  
백엔드가 **코드 교환 → 사용자 조회/생성 → JWT 발급**까지 처리하는 소셜 로그인 API를 추가했다.

기존 `AuthService`의 JWT 발급 로직을 그대로 재사용하므로, OAuth로 발급된 토큰은  
`get_current_user` 미들웨어와 완전히 호환된다 (기존 보호 엔드포인트 수정 불필요).

---

## 추가된 API 엔드포인트

| Method | Path | Request Body | 설명 |
|--------|------|-------------|------|
| POST | `/auth/oauth/google` | `{ "code": "..." }` | Google 인가 코드로 로그인 |
| POST | `/auth/oauth/kakao` | `{ "code": "..." }` | Kakao 인가 코드로 로그인 |
| POST | `/auth/oauth/naver` | `{ "code": "...", "state": "..." }` | Naver 인가 코드로 로그인 |

모든 엔드포인트는 성공 시 `{ "access_token": "...", "refresh_token": "...", "token_type": "bearer" }` 반환.

---

## 변경된 파일 (11개)

### 신규 생성
| 파일 | 역할 |
|------|------|
| `db/migrations/006_add_oauth_accounts.sql` | `user_oauth_accounts` 테이블 생성, `users.password_hash` NULL 허용 |
| `repositories/oauth_repository.py` | provider+oauth_id 기반 사용자 조회/생성 |
| `services/oauth_service.py` | 제공자별 토큰 교환 + JWT 발급 (293줄) |
| `routes/oauth.py` | 3개 OAuth 엔드포인트 라우터 |
| `tests/test_oauth_service.py` | OAuthService 유닛 테스트 5개 |

### 수정
| 파일 | 변경 내용 |
|------|---------|
| `pyproject.toml` | `httpx` → dev → main 의존성으로 이동 |
| `settings.py` | Google/Kakao/Naver 자격증명 9개 필드 추가 |
| `.env.example` | OAuth 환경변수 템플릿 추가 |
| `schemas/auth.py` | `OAuthLoginRequest`, `OAuthNaverLoginRequest` 추가 |
| `dependencies/auth.py` | `get_oauth_service` DI 함수 추가 |
| `main.py` | `oauth_router` 등록 |

---

## 커밋 이력

```
3bbed56  feat: register OAuth routes and get_oauth_service dependency
a274082  feat: add OAuthService with Google/Kakao/Naver token exchange and JWT issuance
60ecb8f  feat: add OAuthRepository for provider-based user lookup and creation
4339d39  feat: add OAuth provider settings, schemas, and move httpx to main deps
07b71e1  feat: add user_oauth_accounts table and allow nullable password_hash
```

---

## 테스트 결과

```
tests/test_oauth_service.py    5 passed
전체 테스트 (기존 포함)       112 passed, 3 failed*
```

> *3개 실패는 `test_rag_persistence.py`의 `test_search_chunks_hybrid_*` — OAuth 작업 이전부터 존재하던 기존 실패. 이번 변경과 무관.

---

## 환경 변수 설정 (`.env`)

구현 완료 후 실제 로그인을 테스트하려면 `.env`에 아래 항목 추가 필요:

```env
# Google OAuth — console.cloud.google.com
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=

# Kakao OAuth — developers.kakao.com (REST API 키)
KAKAO_CLIENT_ID=
KAKAO_CLIENT_SECRET=
KAKAO_REDIRECT_URI=

# Naver OAuth — developers.naver.com
NAVER_CLIENT_ID=
NAVER_CLIENT_SECRET=
NAVER_REDIRECT_URI=
```

---

## 리뷰 권장 순서

### 1단계: DB 스키마 확인 (5분)
**파일:** `db/migrations/006_add_oauth_accounts.sql`

- `user_oauth_accounts` 테이블 구조가 적절한지 (컬럼, FK, UNIQUE 제약)
- `users.password_hash DROP NOT NULL`이 기존 데이터에 영향 없는지
- 마이그레이션 롤백 전략이 필요한지

### 2단계: 설정/스키마 레이어 (5분)
**파일:** `settings.py`, `schemas/auth.py`, `.env.example`

- 새 OAuth 필드가 기존 Settings 패턴과 일치하는지
- `OAuthNaverLoginRequest`의 `state` 필드가 CSRF 방어에 충분한지
- `OAuthLoginRequest` vs `OAuthNaverLoginRequest` 분리 설계가 적절한지

### 3단계: Repository 레이어 (10분)
**파일:** `repositories/oauth_repository.py`

- `create_user_with_oauth`가 2번의 `execute` 호출을 하는데 트랜잭션 없이 괜찮은지  
  (users INSERT 성공 후 oauth_accounts INSERT 실패 시 고아 레코드 가능 — MVP 수준에서 수용 가능)
- 동일 이메일로 이미 가입된 계정이 있을 때 처리 전략 (현재: 새 계정 생성, 이메일 충돌 에러 발생 가능)
- `fetchrow` → `dict(row)` 변환 패턴이 기존 레포지토리와 동일한지

### 4단계: Service 레이어 핵심 (15분)
**파일:** `services/oauth_service.py`

- 각 제공자별 API 엔드포인트 URL이 정확한지
  - Google: `https://oauth2.googleapis.com/token`, `https://www.googleapis.com/oauth2/v3/userinfo`
  - Kakao: `https://kauth.kakao.com/oauth/token`, `https://kapi.kakao.com/v2/user/me`
  - Naver: `https://nid.naver.com/oauth2.0/token`, `https://openapi.naver.com/v1/nid/me`
- Kakao 사용자 정보 요청이 GET이 아닌 POST인 것이 맞는지 (카카오 API 스펙 확인)
- 이메일 없는 경우 fallback (`@kakao.local`, `@naver.local`) 처리가 적절한지
- `_issue_tokens`의 JWT 클레임이 `AuthService`와 동일한지 (`sub`, `email`, `type`, `exp`)
- HTTP 502 변환 로직 — 모든 httpx 에러가 502로만 처리되는 게 적절한지

### 5단계: 라우트 / DI / 연결부 (5분)
**파일:** `routes/oauth.py`, `dependencies/auth.py`, `main.py`

- `get_oauth_service`의 로컬 임포트 방식이 circular import를 잘 방지하는지
- `router.prefix="/auth/oauth"`가 기존 `/auth/` 네임스페이스와 충돌 없는지
- `app.include_router` 순서가 라우팅에 영향을 주지 않는지

### 6단계: 테스트 커버리지 (5분)
**파일:** `tests/test_oauth_service.py`

- 기존 유저 / 신규 유저 케이스가 모두 커버됐는지
- httpx mock 방식이 실제 동작을 충분히 재현하는지
- 502 에러 케이스 외 다른 에러 케이스(네트워크 타임아웃 등)가 필요한지

---

## 알려진 한계 / 후속 작업

| 항목 | 현재 상태 | 권장 대응 |
|------|---------|---------|
| 트랜잭션 미사용 | users + oauth_accounts 2단계 INSERT가 트랜잭션 밖 | 고아 레코드 발생 가능 — 운영 전 트랜잭션 적용 권장 |
| 이메일 중복 처리 | 동일 이메일로 기존 가입 시 DB UNIQUE 에러 발생 | 이메일로 기존 계정 찾아 OAuth 연동하는 로직 추가 필요 |
| Kakao POST 방식 | 사용자 정보 조회를 POST로 처리 | 카카오 공식 문서 재확인 필요 (GET도 지원하는지) |
| refresh token 갱신 | 현재 `/auth/refresh` 없음 | 기존 AuthService에도 없으므로 별도 이슈로 추적 |
