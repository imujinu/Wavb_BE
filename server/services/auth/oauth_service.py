"""OAuth 제공자별 인가 코드 교환 및 JWT 발급 서비스"""
import logging
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from repositories.oauth_repository import OAuthRepository
from schemas.auth import TokenResponse
from settings import Settings, get_settings
from utils import jwt_utils


logger = logging.getLogger(__name__)


class OAuthService:
    """
    기능 요약: Google / Kakao / Naver OAuth 인가 코드를 받아 제공자 토큰을 교환하고,
               사용자 계정을 조회 또는 생성한 뒤 내부 JWT를 발급한다.

    기능 흐름:
        1. provider별 login_with_* 메서드가 인가 코드를 받음
        2. _post / _get 헬퍼로 제공자 API 호출 (토큰 교환 → 사용자 정보)
        3. _find_or_create로 DB 계정 조회 또는 신규 생성
        4. _issue_tokens로 내부 JWT(access + refresh) 발급 후 반환

    파라미터:
        repository: OAuthRepository 인스턴스 (테스트 시 Fake 구현체 주입 가능)
        settings: 애플리케이션 설정 (기본값: get_settings())
    """

    def __init__(self, repository: OAuthRepository, settings: Settings | None = None):
        self._repository = repository
        self._settings = settings or get_settings()

    async def login_with_google(self, code: str) -> TokenResponse:
        """
        기능 요약: Google 인가 코드로 액세스 토큰을 교환하고 사용자 정보를 조회하여 JWT를 발급한다.

        기능 흐름:
            1. 인가 코드 → Google 토큰 엔드포인트에 POST하여 액세스 토큰 획득
            2. 액세스 토큰 → Google userinfo 엔드포인트에 GET하여 사용자 정보 획득
            3. DB에서 계정 조회 또는 신규 생성
            4. 내부 JWT 발급 후 반환

        파라미터:
            code: Google OAuth 인가 코드 (예: "4/0AX4XfWi...")
        """
        self._ensure_google_settings()
        token_payload = {
            "code": code,
            "client_id": self._settings.google_client_id,
            "client_secret": self._settings.google_client_secret,
            "grant_type": "authorization_code",
        }
        if self._settings.google_redirect_uri.strip():
            token_payload["redirect_uri"] = self._settings.google_redirect_uri

        async with httpx.AsyncClient() as client:
            # 1. 인가 코드 → Google 액세스 토큰
            token_resp = await self._post(
                client, "https://oauth2.googleapis.com/token",
                data=token_payload,
                error_label="Google 토큰 교환",
            )
            # 2. 액세스 토큰 → 사용자 정보
            info = await self._get(
                client, "https://www.googleapis.com/oauth2/v3/userinfo",
                token=token_resp["access_token"],
                error_label="Google 사용자 정보",
            )
        # 3. DB 계정 조회 또는 신규 생성
        user_id = await self._find_or_create(
            provider="google",
            oauth_id=info["sub"],
            email=info.get("email", ""),
            nickname=info.get("name", info.get("email", "").split("@")[0]),
            provider_data=info,
        )
        # 4. 내부 JWT 발급
        return self._issue_tokens(user_id, info.get("email", ""))

    async def login_with_kakao(self, code: str) -> TokenResponse:
        """
        기능 요약: Kakao 인가 코드로 액세스 토큰을 교환하고 사용자 정보를 조회하여 JWT를 발급한다.

        기능 흐름:
            1. 인가 코드 → Kakao 토큰 엔드포인트에 POST하여 액세스 토큰 획득
            2. 액세스 토큰 → Kakao 사용자 정보 엔드포인트에 POST하여 사용자 정보 획득
            3. DB에서 계정 조회 또는 신규 생성
            4. 내부 JWT 발급 후 반환

        파라미터:
            code: Kakao OAuth 인가 코드 (예: "xZ1a2b3c...")
        """
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
            # 2. 액세스 토큰 → 사용자 정보 (Kakao는 POST 방식 사용)
            info = await self._post(
                client, "https://kapi.kakao.com/v2/user/me",
                data={},
                error_label="Kakao 사용자 정보",
                headers={"Authorization": f"Bearer {token_resp['access_token']}"},
            )
        # 3. kakao_account 구조에서 이메일/닉네임 추출
        kakao_account = info.get("kakao_account", {})
        oauth_id = str(info["id"])
        email = kakao_account.get("email", f"{oauth_id}@kakao.local")
        nickname = kakao_account.get("profile", {}).get("nickname", f"kakao_{oauth_id}")
        # 4. DB 계정 조회 또는 신규 생성
        user_id = await self._find_or_create("kakao", oauth_id, email, nickname, info)
        # 5. 내부 JWT 발급
        return self._issue_tokens(user_id, email)

    async def login_with_naver(self, code: str, state: str) -> TokenResponse:
        """
        기능 요약: Naver 인가 코드와 state로 액세스 토큰을 교환하고 사용자 정보를 조회하여 JWT를 발급한다.

        기능 흐름:
            1. 인가 코드 + state → Naver 토큰 엔드포인트에 POST하여 액세스 토큰 획득
            2. 액세스 토큰 → Naver 사용자 정보 엔드포인트에 GET하여 사용자 정보 획득
            3. DB에서 계정 조회 또는 신규 생성
            4. 내부 JWT 발급 후 반환

        파라미터:
            code: Naver OAuth 인가 코드 (예: "abc123...")
            state: CSRF 방어용 상태값 (예: "random_state_string")
        """
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
        # 3. response 키 아래에 실제 사용자 정보가 위치 (Naver API 구조)
        naver_resp = info.get("response", {})
        oauth_id = naver_resp["id"]
        email = naver_resp.get("email", f"{oauth_id}@naver.local")
        nickname = naver_resp.get("name", f"naver_{oauth_id}")
        # 4. DB 계정 조회 또는 신규 생성
        user_id = await self._find_or_create("naver", oauth_id, email, nickname, naver_resp)
        # 5. 내부 JWT 발급
        return self._issue_tokens(user_id, email)

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        data: dict,
        error_label: str,
        headers: dict | None = None,
    ) -> dict:
        """
        기능 요약: HTTP POST 요청을 전송하고 에러 발생 시 502로 변환한다.
        — 제공자 API 호출 실패를 클라이언트에게 upstream 오류로 일관되게 전달하기 위해 존재한다.

        파라미터:
            client: httpx.AsyncClient 인스턴스
            url: 요청 대상 URL (예: "https://oauth2.googleapis.com/token")
            data: form-encoded 요청 본문 (예: {"code": "...", "grant_type": "authorization_code"})
            error_label: 오류 메시지에 포함할 레이블 (예: "Google 토큰 교환")
            headers: 추가 헤더 (예: {"Content-Type": "application/x-www-form-urlencoded"})
        """
        try:
            resp = await client.post(url, data=data, headers=headers or {})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            detail = self._provider_error_detail(error_label, e.response)
            logger.warning("%s", detail)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=detail,
            )
            raise HTTPException(502, f"{error_label} 실패: {e.response.status_code}")

    def _ensure_google_settings(self) -> None:
        missing = [
            name
            for name, value in {
                "GOOGLE_CLIENT_ID": self._settings.google_client_id,
                "GOOGLE_CLIENT_SECRET": self._settings.google_client_secret,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Missing Google OAuth settings: {', '.join(missing)}",
            )

    def _provider_error_detail(
        self,
        error_label: str,
        response: httpx.Response,
    ) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"{error_label} failed: {response.status_code}"

        error = payload.get("error")
        description = payload.get("error_description")
        if error and description:
            return f"{error_label} failed: {error} - {description}"
        if error:
            return f"{error_label} failed: {error}"
        return f"{error_label} failed: {response.status_code}"

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        token: str,
        error_label: str,
    ) -> dict:
        """
        기능 요약: Bearer 토큰을 사용한 HTTP GET 요청을 전송하고 에러 발생 시 502로 변환한다.
        — 사용자 정보 조회 등 인증된 GET 요청을 일관된 인터페이스로 처리하기 위해 존재한다.

        파라미터:
            client: httpx.AsyncClient 인스턴스
            url: 요청 대상 URL (예: "https://www.googleapis.com/oauth2/v3/userinfo")
            token: Bearer 액세스 토큰 (예: "ya29.a0AfH6...")
            error_label: 오류 메시지에 포함할 레이블 (예: "Google 사용자 정보")
        """
        try:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"{error_label} 실패: {e.response.status_code}")

    async def _find_or_create(
        self,
        provider: str,
        oauth_id: str,
        email: str,
        nickname: str,
        provider_data: dict,
    ) -> UUID:
        """
        기능 요약: OAuth 계정으로 기존 사용자를 조회하고, 없으면 신규 계정을 생성한다.
        — 소셜 로그인의 "최초 로그인 시 자동 가입" 패턴을 단일 진입점으로 캡슐화하기 위해 존재한다.

        기능 흐름:
            1. provider + oauth_id로 기존 사용자 조회
            2. 존재하면 해당 user_id 반환
            3. 미존재 시 신규 users + user_oauth_accounts 레코드 생성 후 user_id 반환

        파라미터:
            provider: OAuth 제공자 식별자 (예: "google", "kakao", "naver")
            oauth_id: 제공자가 발급한 사용자 고유 ID (예: "1234567890")
            email: 사용자 이메일 (예: "user@gmail.com")
            nickname: 사용자 닉네임 (예: "홍길동")
            provider_data: 제공자로부터 받은 전체 사용자 정보 dict
        """
        # 1. 기존 OAuth 연동 계정 조회
        existing = await self._repository.get_user_by_oauth(provider, oauth_id)
        if existing:
            # 2. 기존 계정 존재 시 user_id 반환
            return existing["id"]
        # 3. 신규 계정 생성 후 user_id 반환
        return await self._repository.create_user_with_oauth(
            email=email,
            nickname=nickname,
            provider=provider,
            oauth_id=oauth_id,
            provider_data=provider_data,
        )

    def _issue_tokens(self, user_id: UUID, email: str) -> TokenResponse:
        """
        기능 요약: user_id와 email을 담은 access/refresh JWT를 생성하여 반환한다.
        — jwt_utils.create_token_pair에 위임하여 토큰 생성 로직을 단일 지점으로 통합한다.

        기능 흐름:
            1. jwt_utils.create_token_pair 호출로 access/refresh 토큰 쌍 생성 후 반환

        파라미터:
            user_id: 내부 사용자 UUID (예: UUID("a1b2c3..."))
            email: 사용자 이메일 (예: "user@gmail.com")
        """
        # 1. jwt_utils를 통해 access/refresh 토큰 쌍 생성 후 반환
        return jwt_utils.create_token_pair(user_id, email, self._settings)
