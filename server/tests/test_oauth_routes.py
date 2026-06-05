from fastapi.testclient import TestClient

from main import app
from routes import oauth
from schemas.auth import TokenResponse


client = TestClient(app)


class FakeOAuthService:
    def __init__(self) -> None:
        self.google_code = None

    async def login_with_google(self, code: str) -> TokenResponse:
        self.google_code = code
        return TokenResponse(
            access_token="access",
            refresh_token="refresh",
        )


def test_google_oauth_route_accepts_server_auth_code_alias() -> None:
    fake_service = FakeOAuthService()
    app.dependency_overrides[oauth.get_oauth_service] = lambda: fake_service

    try:
        response = client.post(
            "/auth/oauth/google",
            json={"serverAuthCode": "mobile-code"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert fake_service.google_code == "mobile-code"
    assert response.json() == {
        "access_token": "access",
        "refresh_token": "refresh",
        "token_type": "bearer",
    }


def test_google_oauth_route_rejects_missing_code() -> None:
    fake_service = FakeOAuthService()
    app.dependency_overrides[oauth.get_oauth_service] = lambda: fake_service

    try:
        response = client.post(
            "/auth/oauth/google",
            json={"idToken": "not-supported-by-this-endpoint"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert fake_service.google_code is None
