"""待办模块（6 个接口 + 筛选 + 排序 + 软删除 + 用户隔离）。

写测试时的三条自定规则：

1. **只断言行为，不断言实现**：查的是「接口返回什么」「库里变成什么」，不去 mock 内部函数。
2. **测试数据要能互相验算**：数量、金额、顺序都取小整数，一眼能看出对不对。
3. **每个「坑」单独一条**：时区、LIKE 通配符、半开区间、PATCH 三态、幂等、隔离，
   这些是最容易改坏的地方，一条一个用例。
"""

from datetime import datetime, timedelta

from conftest import API, make_task, new_session
from core.time import now_utc


def beijing(offset_days: int = 0, hour: int = 12) -> datetime:
    """北京时间「今天 + offset_days」的 hour 点，返回可直接入库的 naive UTC。"""
    base = (now_utc() + timedelta(hours=8)).replace(minute=0, second=0, microsecond=0)
    return base.replace(hour=hour) + timedelta(days=offset_days) - timedelta(hours=8)


# ---------- 列表：分页与筛选 ----------


def test_list_returns_page_meta(client, db):
    for i in range(3):
        make_task(db, title=f"待办 {i}")

    res = client.get(f"{API}/tasks")

    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert (body["page"], body["size"], body["pages"]) == (1, 20, 1)
    assert len(body["items"]) == 3


def test_list_paginates_and_reports_pages(client, db):
    for i in range(5):
        make_task(db, title=f"待办 {i}", due_at=beijing(i))

    res = client.get(f"{API}/tasks", params={"page": 3, "size": 2})

    body = res.json()
    assert body["total"] == 5
    assert body["pages"] == 3
    assert len(body["items"]) == 1


def test_list_filters_by_multiple_status(client, db):
    make_task(db, title="A", status="todo")
    make_task(db, title="B", status="doing")
    make_task(db, title="C", status="done")

    res = client.get(f"{API}/tasks", params=[("status", "todo"), ("status", "done")])

    titles = {item["title"] for item in res.json()["items"]}
    assert titles == {"A", "C"}


def test_list_filters_by_priority(client, db):
    make_task(db, title="高", priority="high")
    make_task(db, title="低", priority="low")

    body = client.get(f"{API}/tasks", params={"priority": "high"}).json()

    assert [item["title"] for item in body["items"]] == ["高"]


def test_list_keyword_searches_title_and_description(client, db):
    make_task(db, title="买牛奶", description=None)
    make_task(db, title="其他", description="顺便买牛奶")
    make_task(db, title="无关")

    body = client.get(f"{API}/tasks", params={"keyword": "牛奶"}).json()

    assert body["total"] == 2


def test_list_keyword_escapes_like_wildcards(client, db):
    """`%` 不能当通配符：搜 `%` 只该命中标题里真的有 % 的那条。"""
    make_task(db, title="折扣 50% 的券")
    make_task(db, title="普通待办")

    body = client.get(f"{API}/tasks", params={"keyword": "%"}).json()

    assert [item["title"] for item in body["items"]] == ["折扣 50% 的券"]


def test_list_date_range_is_half_open(client, db):
    """due_from 当天 00:00 起、due_to 次日 00:00 止（半开），所以边界日期算在内、多一天不算。"""
    today = now_utc() + timedelta(hours=8)
    this_month_first = today.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
    make_task(db, title="月初", due_at=this_month_first - timedelta(hours=8))
    make_task(db, title="上月末", due_at=this_month_first - timedelta(days=1, hours=8))

    day = this_month_first.date().isoformat()
    body = client.get(f"{API}/tasks", params={"due_from": day, "due_to": day}).json()

    assert [item["title"] for item in body["items"]] == ["月初"]


def test_list_overdue_only(client, db):
    make_task(db, title="逾期", due_at=beijing(-2))
    make_task(db, title="未到期", due_at=beijing(3))

    body = client.get(f"{API}/tasks", params={"overdue": "true"}).json()

    assert [item["title"] for item in body["items"]] == ["逾期"]
    assert body["items"][0]["is_overdue"] is True


def test_list_filters_unclassified_by_zero(client, db):
    """category_id=0 是「未分类」，是有效筛选值，不能被当成"没传"。"""
    make_task(db, title="未分类", category_id=0)
    make_task(db, title="工作", category_id=15)

    body = client.get(f"{API}/tasks", params={"category_id": 0}).json()

    assert [item["title"] for item in body["items"]] == ["未分类"]


# ---------- 列表：排序 ----------


def test_list_sorts_by_priority_desc(client, db):
    make_task(db, title="低", priority="low")
    make_task(db, title="高", priority="high")
    make_task(db, title="中", priority="medium")

    body = client.get(
        f"{API}/tasks", params={"sort": "priority", "order": "desc"}
    ).json()

    assert [item["title"] for item in body["items"]] == ["高", "中", "低"]


def test_list_sorts_by_created_at_desc(client, db):
    first = make_task(db, title="先建")
    second = make_task(db, title="后建")

    body = client.get(
        f"{API}/tasks", params={"sort": "created_at", "order": "desc"}
    ).json()

    assert [item["id"] for item in body["items"]] == [second.id, first.id]


def test_list_keeps_tasks_without_due_date_last(client, db):
    make_task(db, title="有期限", due_at=beijing(1))
    make_task(db, title="没期限", due_at=None)

    body = client.get(f"{API}/tasks", params={"sort": "due_at", "order": "asc"}).json()

    assert [item["title"] for item in body["items"]] == ["有期限", "没期限"]


def test_list_rejects_unknown_sort(client):
    res = client.get(f"{API}/tasks", params={"sort": "title"})

    assert res.status_code == 422
    assert res.json()["code"] == "VALIDATION_ERROR"


def test_list_rejects_reversed_date_range(client):
    res = client.get(
        f"{API}/tasks", params={"due_from": "2026-12-31", "due_to": "2026-12-01"}
    )

    assert res.status_code == 422
    assert res.json()["message"] == "due_from 不能晚于 due_to"


# ---------- 新建 ----------


def test_create_task_uses_defaults(client):
    res = client.post(f"{API}/tasks", json={"title": "只有标题"})

    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "todo"
    assert body["priority"] == "medium"
    assert body["category_id"] == 0
    assert body["category_name"] is None
    assert body["due_at"] is None
    assert body["is_overdue"] is False
    assert body["id"] > 0


def test_create_task_round_trips_timezone(client):
    res = client.post(
        f"{API}/tasks",
        json={"title": "交房租", "due_at": "2026-12-01T09:30:00+08:00"},
    )

    assert res.status_code == 201
    assert res.json()["due_at"] == "2026-12-01T09:30:00+08:00"

    with new_session() as session:
        from modules.lifeweb.tasks.model import Task

        task = session.get(Task, res.json()["id"])
        assert task.due_at.isoformat(sep=" ") == "2026-12-01 01:30:00"


def test_create_task_strips_whitespace(client):
    res = client.post(f"{API}/tasks", json={"title": "  去超市  "})

    assert res.json()["title"] == "去超市"


def test_create_task_rejects_blank_title(client):
    res = client.post(f"{API}/tasks", json={"title": "   "})

    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["detail"]["errors"][0]["loc"] == ["body", "title"]


def test_create_task_rejects_unknown_priority(client):
    res = client.post(f"{API}/tasks", json={"title": "x", "priority": "urgent"})

    assert res.status_code == 422


def test_create_task_rejects_unknown_category(client):
    res = client.post(f"{API}/tasks", json={"title": "x", "category_id": 999999})

    assert res.status_code == 422
    assert res.json()["message"] == "分类不存在"


def test_create_task_rejects_category_from_another_scope(client):
    """id=1 是 expense 域的分类，不能挂到待办上。"""
    res = client.post(f"{API}/tasks", json={"title": "x", "category_id": 1})

    assert res.status_code == 422
    assert res.json()["message"] == "分类不存在"


# ---------- 详情 ----------


def test_get_task_detail(client, db):
    task = make_task(db, title="看详情", category_id=15)

    res = client.get(f"{API}/tasks/{task.id}")

    assert res.status_code == 200
    body = res.json()
    assert body["title"] == "看详情"
    assert body["category_name"] == "工作"


def test_get_task_404_uses_unified_body(client):
    res = client.get(f"{API}/tasks/999999")

    assert res.status_code == 404
    assert res.json() == {
        "code": "NOT_FOUND",
        "message": "待办不存在",
        "request_id": None,
        "detail": None,
    }


def test_get_task_of_another_user_is_404_not_403(client, db):
    """别人的待办返回 404：403 会泄露「这条数据存在」。"""
    other = make_task(db, user_id=999, title="别人的")

    assert client.get(f"{API}/tasks/{other.id}").status_code == 404


def test_get_task_rejects_non_integer_id(client):
    res = client.get(f"{API}/tasks/abc")

    assert res.status_code == 422
    assert res.json()["detail"]["errors"][0]["loc"] == ["path", "task_id"]


# ---------- 局部更新 ----------


def test_patch_only_changes_given_fields(client, db):
    task = make_task(db, title="原标题", description="原备注", priority="high")

    res = client.patch(f"{API}/tasks/{task.id}", json={"title": "新标题"})

    body = res.json()
    assert (body["title"], body["description"], body["priority"]) == (
        "新标题",
        "原备注",
        "high",
    )


def test_patch_empty_body_is_rejected(client, db):
    task = make_task(db)

    res = client.patch(f"{API}/tasks/{task.id}", json={})

    assert res.status_code == 422
    assert "没有需要修改的字段" in res.json()["detail"]["errors"][0]["msg"]


def test_patch_rejects_null_on_required_field(client, db):
    task = make_task(db)

    res = client.patch(f"{API}/tasks/{task.id}", json={"title": None})

    assert res.status_code == 422
    assert "title 不能为 null" in res.json()["detail"]["errors"][0]["msg"]


def test_patch_accepts_null_to_clear_description(client, db):
    task = make_task(db, description="要被清空")

    res = client.patch(f"{API}/tasks/{task.id}", json={"description": None})

    assert res.json()["description"] is None

    with new_session() as session:
        from modules.lifeweb.tasks.model import Task

        assert session.get(Task, task.id).description is None


def test_patch_404_for_missing_task(client):
    res = client.patch(f"{API}/tasks/999999", json={"title": "x"})

    assert res.status_code == 404


# ---------- 切换状态 ----------


def test_status_done_is_idempotent(client, db):
    """重复标完成不能刷新 completed_at —— 否则「完成时间」会变成最后一次点击时间。"""
    task = make_task(db, title="写周报")

    first = client.patch(f"{API}/tasks/{task.id}/status", json={"status": "done"}).json()
    second = client.patch(f"{API}/tasks/{task.id}/status", json={"status": "done"}).json()

    assert first["status"] == "done"
    assert first["completed_at"] is not None
    assert second["completed_at"] == first["completed_at"]


def test_status_back_to_todo_clears_completed_at(client, db):
    task = make_task(db, title="来回切")
    client.patch(f"{API}/tasks/{task.id}/status", json={"status": "done"})

    body = client.patch(f"{API}/tasks/{task.id}/status", json={"status": "todo"}).json()

    assert body["status"] == "todo"
    assert body["completed_at"] is None


def test_status_rejects_unknown_value(client, db):
    task = make_task(db)

    res = client.patch(f"{API}/tasks/{task.id}/status", json={"status": "finished"})

    assert res.status_code == 422


# ---------- 删除（硬删） ----------


def test_delete_returns_204_and_removes_row(client, db):
    task = make_task(db, title="要删的")

    res = client.delete(f"{API}/tasks/{task.id}")

    assert res.status_code == 204
    assert res.content == b""
    assert client.get(f"{API}/tasks/{task.id}").status_code == 404
    assert client.get(f"{API}/tasks").json()["total"] == 0

    with new_session() as session:
        from modules.lifeweb.tasks.model import Task

        assert session.get(Task, task.id) is None, "硬删除后行必须真的消失"


def test_delete_twice_is_404(client, db):
    task = make_task(db)
    client.delete(f"{API}/tasks/{task.id}")

    assert client.delete(f"{API}/tasks/{task.id}").status_code == 404


def test_delete_another_users_task_does_nothing(client, db):
    other = make_task(db, user_id=999, title="别人的")

    assert client.delete(f"{API}/tasks/{other.id}").status_code == 404

    with new_session() as session:
        from modules.lifeweb.tasks.model import Task

        assert session.get(Task, other.id) is not None, "越权删除不能删掉别人的行"
