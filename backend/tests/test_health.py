from fastapi.testclient import TestClient

from vlytics.api.app import create_app


def test_health_reports_service_and_version() -> None:
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "vlytics-api",
        "version": "0.1.0",
    }
