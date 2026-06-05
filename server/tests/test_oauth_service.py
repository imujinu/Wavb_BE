"""OAuthService 단위 테스트 — FakeOAuthRepository + httpx mock"""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4
import pytest
from fastapi import HTTPException
from services.auth.oauth_service import OAuthService
from schemas.auth import TokenResponse


class FakeOAuthRepository:
    def __init__(self):
        self._accounts: dict[tuple[str, str], dict] = {}
        self._users: dict[UUID, dict] = {}

    async def get_user_by_oauth(self, provider: str, oauth_id: str) -> dict | None:
        return self._accounts.get((provider, oauth_id))

    async def create_user_with_oauth(
        self, email, nickname, provider, oauth_id, provider_data
    ) -> UUID:
        user_id = uuid4()
        user = {"id": user_id, "email": email, "nickname": nickname}
        self._users[user_id] = user
        self._accounts[(provider, oauth_id)] = user
        return user_id

    def seed_user(self, provider: str, oauth_id: str, email: str, nickname: str) -> UUID:
        user_id = uuid4()
        user = {"id": user_id, "email": email, "nickname": nickname}
        self._users[user_id] = user
        self._accounts[(provider, oauth_id)] = user
        return user_id


def make_service() -> OAuthService:
    service = OAuthService.__new__(OAuthService)
    service._repository = FakeOAuthRepository()
    from settings import get_settings
    service._settings = get_settings()
    return service


def make_google_service(*, redirect_uri: str = "") -> OAuthService:
    service = OAuthService.__new__(OAuthService)
    service._repository = FakeOAuthRepository()
    from settings import Settings

    service._settings = Settings(
        GOOGLE_CLIENT_ID="web-client-id",
        GOOGLE_CLIENT_SECRET="web-client-secret",
        GOOGLE_REDIRECT_URI=redirect_uri,
        JWT_SECRET_KEY="test-secret",
    )
    return service


def _mock_response(data: dict) -> MagicMock:
    r = MagicMock()
    r.json.return_value = data
    r.raise_for_status.return_value = None
    return r


@pytest.mark.asyncio
async def test_google_login_creates_new_user_and_returns_tokens():
    service = make_google_service()
    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_response({"access_token": "gtoken"})
    mock_client.get.return_value = _mock_response(
        {"sub": "g_123", "email": "user@gmail.com", "name": "구글 사용자"}
    )
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await service.login_with_google(code="test_code")

    assert isinstance(result, TokenResponse)
    assert result.token_type == "bearer"
    assert len(result.access_token) > 0


@pytest.mark.asyncio
async def test_google_login_returns_tokens_for_existing_user():
    service = make_google_service()
    service._repository.seed_user("google", "g_123", "user@gmail.com", "구글 사용자")
    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_response({"access_token": "gtoken"})
    mock_client.get.return_value = _mock_response(
        {"sub": "g_123", "email": "user@gmail.com", "name": "구글 사용자"}
    )
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await service.login_with_google(code="test_code")

    assert isinstance(result, TokenResponse)


@pytest.mark.asyncio
async def test_google_login_omits_empty_redirect_uri_for_mobile_code_exchange():
    service = make_google_service(redirect_uri="")
    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_response({"access_token": "gtoken"})
    mock_client.get.return_value = _mock_response(
        {"sub": "g_123", "email": "user@gmail.com", "name": "google user"}
    )

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        await service.login_with_google(code="mobile_server_auth_code")

    token_payload = mock_client.post.call_args.kwargs["data"]
    assert token_payload["client_id"] == "web-client-id"
    assert token_payload["client_secret"] == "web-client-secret"
    assert token_payload["code"] == "mobile_server_auth_code"
    assert "redirect_uri" not in token_payload


@pytest.mark.asyncio
async def test_google_login_includes_configured_redirect_uri():
    service = make_google_service(
        redirect_uri="http://localhost:8000/auth/oauth/google/callback"
    )
    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_response({"access_token": "gtoken"})
    mock_client.get.return_value = _mock_response(
        {"sub": "g_123", "email": "user@gmail.com", "name": "google user"}
    )

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        await service.login_with_google(code="web_code")

    token_payload = mock_client.post.call_args.kwargs["data"]
    assert (
        token_payload["redirect_uri"]
        == "http://localhost:8000/auth/oauth/google/callback"
    )


@pytest.mark.asyncio
async def test_kakao_login_creates_new_user_and_returns_tokens():
    service = make_service()
    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        _mock_response({"access_token": "ktoken"}),
        _mock_response({
            "id": 99999,
            "kakao_account": {"email": "user@kakao.com", "profile": {"nickname": "카카오"}},
        }),
    ]
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await service.login_with_kakao(code="kakao_code")

    assert isinstance(result, TokenResponse)


@pytest.mark.asyncio
async def test_naver_login_creates_new_user_and_returns_tokens():
    service = make_service()
    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_response({"access_token": "ntoken"})
    mock_client.get.return_value = _mock_response({
        "resultcode": "00", "message": "success",
        "response": {"id": "naver_789", "email": "user@naver.com", "name": "네이버"},
    })
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await service.login_with_naver(code="naver_code", state="state_xyz")

    assert isinstance(result, TokenResponse)


@pytest.mark.asyncio
async def test_google_login_raises_502_on_provider_error():
    import httpx as _httpx
    service = make_google_service()
    mock_client = AsyncMock()
    response = MagicMock(status_code=400)
    response.json.return_value = {
        "error": "invalid_grant",
        "error_description": "Bad Request",
    }
    mock_client.post.side_effect = _httpx.HTTPStatusError(
        "error", request=MagicMock(), response=response
    )
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc_info:
            await service.login_with_google(code="bad_code")

    assert exc_info.value.status_code == 502
    assert "invalid_grant" in exc_info.value.detail


@pytest.mark.asyncio
async def test_google_login_requires_google_settings():
    service = make_google_service()
    service._settings = service._settings.model_copy(
        update={"google_client_secret": ""}
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.login_with_google(code="code")

    assert exc_info.value.status_code == 500
    assert "GOOGLE_CLIENT_SECRET" in exc_info.value.detail
