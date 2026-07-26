from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.models import BrowserSession, User
from mutiai.models.base import utc_now
from mutiai.security import hash_password, hash_session_token, verify_password


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
    assert response.headers["Content-Language"] == "en-US"
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


def test_error_message_follows_accept_language_without_changing_error_code(
    tmp_path,
) -> None:
    app, _ = auth_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            json={"username": "admin", "password": "wrong"},
        )

    assert response.status_code == 401
    assert response.headers["Content-Language"] == "zh-CN"
    assert response.headers["Vary"] == "Accept-Language"
    assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"
    assert response.json()["message"] == "用户名或密码无效。"


def test_validation_details_are_localized_without_echoing_input(tmp_path) -> None:
    app, _ = auth_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"Accept-Language": "zh"},
            json={"username": "admin", "password": ""},
        )

    assert response.status_code == 422
    payload = response.json()
    assert response.headers["Content-Language"] == "zh-CN"
    assert payload["code"] == "INVALID_REQUEST"
    assert payload["message"] == "请求参数无效。"
    assert all(item["message"] == "文本长度不足。" for item in payload["details"])
    assert "input" not in str(payload).lower()
    assert "password': ''" not in str(payload)


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


def test_account_self_service_requires_authentication(tmp_path) -> None:
    app, _ = auth_app(tmp_path)

    with TestClient(app) as client:
        profile = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "New display name"},
        )
        password = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "123456",
                "new_password": "new-password-789",
            },
        )

    assert profile.status_code == 401
    assert profile.json()["code"] == "AUTH_REQUIRED"
    assert password.status_code == 401
    assert password.json()["code"] == "AUTH_REQUIRED"


def test_display_name_update_is_persisted_and_username_is_immutable(tmp_path) -> None:
    app, _ = auth_app(tmp_path)

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200

        updated = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "  Nexwork Owner  "},
        )
        immutable_username = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "Nexwork Owner", "username": "renamed"},
        )
        current = client.get("/api/v1/auth/me")

    assert updated.status_code == 200
    assert updated.json() == {
        "user_id": updated.json()["user_id"],
        "username": "admin",
        "display_name": "Nexwork Owner",
    }
    assert immutable_username.status_code == 422
    assert immutable_username.json()["code"] == "INVALID_REQUEST"
    assert current.status_code == 200
    assert current.json()["username"] == "admin"
    assert current.json()["display_name"] == "Nexwork Owner"

    with app.state.database.session() as session:
        user = session.scalar(select(User).where(User.username == "admin"))
        assert user is not None
        assert user.display_name == "Nexwork Owner"
        assert session.scalar(select(User).where(User.username == "renamed")) is None


def test_account_self_service_field_validation_does_not_echo_passwords(
    tmp_path,
) -> None:
    app, _ = auth_app(tmp_path)

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200

        empty_display_name = client.patch(
            "/api/v1/auth/me",
            headers={"Accept-Language": "zh-CN"},
            json={"display_name": "   "},
        )
        long_display_name = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "x" * 101},
        )
        short_password = client.post(
            "/api/v1/auth/password",
            headers={"Accept-Language": "zh-CN"},
            json={"current_password": "123456", "new_password": "short"},
        )
        long_password = client.post(
            "/api/v1/auth/password",
            json={"current_password": "123456", "new_password": "x" * 129},
        )

    for response in (
        empty_display_name,
        long_display_name,
        short_password,
        long_password,
    ):
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_REQUEST"
        assert "123456" not in str(response.json())
        assert "'short'" not in str(response.json())
        assert "x" * 129 not in str(response.json())
    assert empty_display_name.headers["Content-Language"] == "zh-CN"
    assert short_password.headers["Content-Language"] == "zh-CN"


def test_password_change_rejects_wrong_current_and_unchanged_password(
    tmp_path,
) -> None:
    app, _ = auth_app(tmp_path)

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200

        wrong_current = client.post(
            "/api/v1/auth/password",
            headers={"Accept-Language": "zh-CN"},
            json={
                "current_password": "wrong-password",
                "new_password": "new-password-789",
            },
        )

        unchanged_password = "current-password-123"
        with app.state.database.session() as session:
            user = session.scalar(select(User).where(User.username == "admin"))
            assert user is not None
            user.password_hash = hash_password(unchanged_password)
            session.commit()
        unchanged = client.post(
            "/api/v1/auth/password",
            headers={"Accept-Language": "zh-CN"},
            json={
                "current_password": unchanged_password,
                "new_password": unchanged_password,
            },
        )

    assert wrong_current.status_code == 400
    assert wrong_current.json()["code"] == "AUTH_CURRENT_PASSWORD_INVALID"
    assert wrong_current.json()["message"] == "当前密码无效。"
    assert unchanged.status_code == 409
    assert unchanged.json()["code"] == "AUTH_NEW_PASSWORD_MUST_DIFFER"
    assert unchanged.json()["message"] == "新密码不能与当前密码相同。"


def test_password_change_rotates_credentials_and_revokes_other_sessions(
    tmp_path,
) -> None:
    app, settings = auth_app(tmp_path)
    new_password = "new-password-789"

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        current_token = client.cookies.get(settings.session_cookie_name)
        assert current_token

        assert client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        other_token = client.cookies.get(settings.session_cookie_name)
        assert other_token and other_token != current_token

        client.cookies.set(settings.session_cookie_name, current_token)
        changed = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "123456",
                "new_password": new_password,
            },
        )
        current_session = client.get("/api/v1/auth/me")

        client.cookies.set(settings.session_cookie_name, other_token)
        revoked_session = client.get("/api/v1/auth/me")
        old_login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "123456"},
        )
        new_login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": new_password},
        )

    assert changed.status_code == 204
    assert current_session.status_code == 200
    assert revoked_session.status_code == 401
    assert revoked_session.json()["code"] == "AUTH_REQUIRED"
    assert old_login.status_code == 401
    assert old_login.json()["code"] == "AUTH_INVALID_CREDENTIALS"
    assert new_login.status_code == 200

    with app.state.database.session() as session:
        user = session.scalar(select(User).where(User.username == "admin"))
        sessions = session.scalars(
            select(BrowserSession).order_by(BrowserSession.created_at)
        ).all()

        assert user is not None
        assert verify_password(new_password, user.password_hash)
        session_by_token = {item.token_hash: item for item in sessions}
        assert session_by_token[hash_session_token(current_token)].revoked_at is None
        assert session_by_token[hash_session_token(other_token)].revoked_at is not None
