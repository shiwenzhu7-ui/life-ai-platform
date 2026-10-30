"""健康检查与统一错误体的冒烟测试。

注意：test_readyz 会连真实 MySQL（本机 lifeai 库），这是 V1.0 阶段的取舍；
后续引入测试库 fixture 后再隔离。
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz_ok() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "server_time" in body
    assert body["server_time"].endswith("+08:00"), "时间应带东八区偏移"


def test_request_id_header_present() -> None:
    resp = client.get("/healthz")
    assert resp.headers.get("X-Request-Id"), "响应应带 X-Request-Id"


def test_request_id_echoed() -> None:
    resp = client.get("/healthz", headers={"X-Request-Id": "fixed-id-123"})
    assert resp.headers["X-Request-Id"] == "fixed-id-123"


def test_readyz_database_ok() -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "database": "ok"}


def test_unknown_route_returns_404() -> None:
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404


def test_openapi_available() -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "LifeAI"
