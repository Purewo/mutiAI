from fastapi.testclient import TestClient

from mutiai.config import Settings
from mutiai.main import create_app


def test_health_returns_configured_environment(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'test.db'}",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "mutiai-core",
        "environment": "test",
    }
