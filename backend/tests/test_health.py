"""健康检查与全局错误体。

这两个端点看起来没什么可测，但它们是运维和前端错误处理的唯一入口：
`/readyz` 挂了说明数据库连不上，统一错误体的形状变了前端所有 `detail` 解析都会坏。
"""

from conftest import API


def test_healthz_ok(client):
    res = client.get("/healthz")

    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_readyz_reports_database(client):
    res = client.get("/readyz")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"


def test_unknown_path_uses_unified_error_body(client):
    """未注册的路径也走统一错误体，而不是 FastAPI 默认的 {"detail": "Not Found"}。"""
    res = client.get("/api/v1/no-such-endpoint")

    assert res.status_code == 404
    body = res.json()
    assert body["code"] == "NOT_FOUND"
    assert body["message"] == "接口不存在"
    assert set(body) == {"code", "message", "request_id", "detail"}


def test_wrong_method_uses_unified_error_body(client):
    """方法不对（405）同样走统一错误体。"""
    res = client.request("PUT", f"{API}/tasks")

    assert res.status_code == 405
    assert res.json()["code"] == "METHOD_NOT_ALLOWED"
