from fastapi.testclient import TestClient
from datetime import timedelta

from sqlalchemy import select

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.models import BrowserSession, User
from mutiai.models.base import utc_now
from mutiai.security import hash_session_token


def auth_app(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'auth.db'}",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
        session_cookie_name="mutiai_test_session",
        session_ttl_seconds=3_600,
    )
    return create_app(settings), settings


def test_login_me_and_logout_revoke_opaque_session(tmp_path) -> None:
    app, settings = auth_app(tmp_path)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "123456"},
        )

        assert login.status_code == 200
        assert login.json()["user"]["username"] == "admin"
        set_cookie = login.headers["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        raw_token = client.cookies.get(settings.session_cookie_name)
        assert raw_token

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json() == login.json()["user"]

        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 204
        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401

    with app.state.database.session() as session:
        user = session.scalar(select(User).where(User.username == "admin"))
        browser_session = session.scalar(select(BrowserSession))

        assert user is not None
        assert user.password_hash != "123456"
        assert user.password_hash.startswith("$argon2")
        assert browser_session is not None
        assert browser_session.token_hash == hash_session_token(raw_token)
        assert browser_session.token_hash != raw_token
        assert browser_session.revoked_at is not None


def test_invalid_credentials_use_stable_error_envelope(tmp_path) -> None:
    app, _ = auth_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"X-Request-ID": "test-request-id"},
            json={"username": "admin", "password": "wrong"},
        )

    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "test-request-id"
    assert response.json() == {
        "code": "AUTH_INVALID_CREDENTIALS",
        "message": "The username or password is invalid.",
        "request_id": "test-request-id",
    }

    with TestClient(app) as client:
        unknown_user = client.post(
            "/api/v1/auth/login",
            headers={"X-Request-ID": "unknown-user-request"},
            json={"username": "missing", "password": "wrong"},
        )

    assert unknown_user.status_code == 401
    assert unknown_user.json()["code"] == "AUTH_INVALID_CREDENTIALS"
    assert unknown_user.json()["message"] == response.json()["message"]


def test_validation_error_never_echoes_password(tmp_path) -> None:
    app, _ = auth_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": ""},
        )

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "INVALID_REQUEST"
    assert "input" not in str(payload).lower()
    assert "password': ''" not in str(payload)


def test_expired_server_session_is_rejected(tmp_path) -> None:
    app, _ = auth_app(tmp_path)

    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "123456"},
            ).status_code
            == 200
        )
        with app.state.database.session() as session:
            browser_session = session.scalar(select(BrowserSession))
            assert browser_session is not None
            browser_session.expires_at = utc_now() - timedelta(seconds=1)
            session.commit()

        response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


def test_bootstrap_admin_is_idempotent_across_restarts(tmp_path) -> None:
    app, settings = auth_app(tmp_path)
    database_url = settings.database_url

    with TestClient(app):
        pass
    second_app = create_app(settings.model_copy(update={"database_url": database_url}))
    with TestClient(second_app):
        pass

    with second_app.state.database.session() as session:
        users = session.scalars(select(User)).all()

    assert len(users) == 1
