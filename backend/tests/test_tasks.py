"""待办模块验收测试 —— 这些用例就是 service 层的实现规格。

当前状态：
    list_tasks / get_task            已实现（示例代码）
    create_task / update_task        待你实现 → 会失败
    change_status / delete_task      待你实现 → 会失败

实现完 service 里的 4 个 TODO 后，本文件应全部通过。
只跑本文件：`pytest tests/test_tasks.py -v`
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.db.session import SessionLocal
from app.models import Task

CST = timezone(timedelta(hours=8))

BASE = "/api/v1/tasks"


def _create(client, **overrides) -> dict:
    """建一条待办并断言成功，返回响应体。"""
    payload = {"title": "买菜"}
    payload.update(overrides)
    resp = client.post(BASE, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- 列表与查询（已实现，应直接通过） ----------


def test_list_empty(client) -> None:
    body = client.get(BASE).json()
    assert body == {"items": [], "total": 0, "page": 1, "size": 20, "pages": 0}


def test_get_not_found(client) -> None:
    resp = client.get(f"{BASE}/999999")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_categories_endpoint(client) -> None:
    resp = client.get("/api/v1/categories", params={"scope": "task"})
    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert "工作" in names and "购物" in names


# ---------- 新建（create_task 待实现） ----------


def test_create_task_ok(client, task_category_ids) -> None:
    shopping_id = task_category_ids["购物"]
    body = _create(
        client,
        title="去超市买牛奶和鸡蛋",
        category_id=shopping_id,
        priority="high",
        due_at="2026-05-02T15:00:00+08:00",
    )
    assert body["title"] == "去超市买牛奶和鸡蛋"
    assert body["category_id"] == shopping_id
    assert body["category_name"] == "购物"
    assert body["priority"] == "high"
    assert body["status"] == "todo"
    assert body["source"] == "manual"
    assert body["is_overdue"] is False


def test_create_task_blank_title(client) -> None:
    resp = client.post(BASE, json={"title": "   "})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_create_task_bad_category(client) -> None:
    resp = client.post(BASE, json={"title": "测试", "category_id": 9999})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "分类不存在" in body["message"]


def test_create_task_remind_after_due(client) -> None:
    resp = client.post(
        BASE,
        json={
            "title": "测试",
            "due_at": "2026-05-02T15:00:00+08:00",
            "remind_at": "2026-05-02T16:00:00+08:00",
        },
    )
    assert resp.status_code == 422
    assert "提醒时间必须早于截止时间" in str(resp.json())


def test_timezone_roundtrip(client) -> None:
    """带 +08:00 的时间写入后，读出来仍是同一时刻、同一偏移。"""
    body = _create(client, due_at="2026-05-02T15:00:00+08:00")
    assert body["due_at"] == "2026-05-02T15:00:00+08:00"

    fetched = client.get(f"{BASE}/{body['id']}").json()
    assert fetched["due_at"] == "2026-05-02T15:00:00+08:00"


# ---------- 修改（update_task 待实现） ----------


def test_update_partial(client) -> None:
    task = _create(client, title="原标题", description="原描述", priority="low")
    resp = client.patch(f"{BASE}/{task['id']}", json={"title": "新标题"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "新标题"
    assert body["description"] == "原描述", "未传的字段不应被改动"
    assert body["priority"] == "low"


def test_update_clear_due(client) -> None:
    """显式传 null 应清空字段 —— 与「没传这个键」是不同的语义。"""
    task = _create(client, due_at="2026-05-02T15:00:00+08:00")
    resp = client.patch(f"{BASE}/{task['id']}", json={"due_at": None})
    assert resp.status_code == 200
    assert resp.json()["due_at"] is None


def test_update_not_found(client) -> None:
    resp = client.patch(f"{BASE}/999999", json={"title": "改不到"})
    assert resp.status_code == 404


# ---------- 状态切换（change_status 待实现） ----------


def test_status_to_done_sets_completed_at(client) -> None:
    task = _create(client)
    resp = client.patch(f"{BASE}/{task['id']}/status", json={"status": "done"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    assert resp.json()["completed_at"] is not None


def test_status_reopen_clears_completed_at(client) -> None:
    task = _create(client)
    client.patch(f"{BASE}/{task['id']}/status", json={"status": "done"})
    resp = client.patch(f"{BASE}/{task['id']}/status", json={"status": "todo"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "todo"
    assert resp.json()["completed_at"] is None


def test_status_idempotent(client) -> None:
    """把已完成的任务再设为已完成，completed_at 不应被刷新。"""
    task = _create(client)
    first = client.patch(f"{BASE}/{task['id']}/status", json={"status": "done"}).json()
    second = client.patch(f"{BASE}/{task['id']}/status", json={"status": "done"}).json()
    assert first["completed_at"] == second["completed_at"]


def test_status_invalid_value(client) -> None:
    task = _create(client)
    resp = client.patch(f"{BASE}/{task['id']}/status", json={"status": "flying"})
    assert resp.status_code == 422


# ---------- 删除（delete_task 待实现） ----------


def test_delete_then_get_404(client) -> None:
    task = _create(client)
    assert client.delete(f"{BASE}/{task['id']}").status_code == 204
    assert client.get(f"{BASE}/{task['id']}").status_code == 404


def test_delete_twice_404(client) -> None:
    task = _create(client)
    assert client.delete(f"{BASE}/{task['id']}").status_code == 204
    assert client.delete(f"{BASE}/{task['id']}").status_code == 404


# ---------- 筛选、分页、隔离 ----------


def test_list_filter_by_status(client) -> None:
    done_task = _create(client, title="要完成的")
    _create(client, title="不管的")
    client.patch(f"{BASE}/{done_task['id']}/status", json={"status": "done"})

    body = client.get(BASE, params={"status": "done"}).json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "要完成的"


def test_list_overdue_filter(client) -> None:
    past = (datetime.now(CST) - timedelta(days=1)).isoformat()
    future = (datetime.now(CST) + timedelta(days=1)).isoformat()
    _create(client, title="逾期的", due_at=past)
    _create(client, title="没逾期", due_at=future)

    body = client.get(BASE, params={"overdue": "true"}).json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "逾期的"
    assert body["items"][0]["is_overdue"] is True


def test_pagination(client) -> None:
    for index in range(5):
        _create(client, title=f"任务{index}")

    page1 = client.get(BASE, params={"page": 1, "size": 2}).json()
    assert page1["total"] == 5
    assert page1["pages"] == 3
    assert len(page1["items"]) == 2

    page3 = client.get(BASE, params={"page": 3, "size": 2}).json()
    assert len(page3["items"]) == 1


def test_keyword_search(client) -> None:
    _create(client, title="买牛奶")
    _create(client, title="写周报")
    body = client.get(BASE, params={"keyword": "牛奶"}).json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "买牛奶"


def test_other_user_task_is_invisible(client) -> None:
    """数据隔离：别的用户的数据既查不到、也读不到（只能是 404）。"""
    db = SessionLocal()
    try:
        foreign = Task(
            user_id=999,
            title="别人的待办",
            category_id=0,
            priority="medium",
            status="todo",
            source="manual",
        )
        db.add(foreign)
        db.commit()
        db.refresh(foreign)
        foreign_id = foreign.id
    finally:
        db.close()

    try:
        assert client.get(f"{BASE}/{foreign_id}").status_code == 404
        listing = client.get(BASE).json()
        assert all(item["id"] != foreign_id for item in listing["items"])
        assert client.patch(f"{BASE}/{foreign_id}", json={"title": "x"}).status_code == 404
        assert client.delete(f"{BASE}/{foreign_id}").status_code == 404
    finally:
        db = SessionLocal()
        try:
            db.execute(delete(Task).where(Task.user_id == 999))
            db.commit()
        finally:
            db.close()
