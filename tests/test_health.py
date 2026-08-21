from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["components"]["database"]["status"] == "ok"
    assert payload["components"]["worker"]["status"] == "disabled"
    assert payload["components"]["hermes"]["status"] == "disabled"
    assert payload["components"]["delivery"]["status"] == "disabled"


def test_health_rejects_post(client: TestClient) -> None:
    response = client.post("/health")

    assert response.status_code == 405
