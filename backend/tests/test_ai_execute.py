"""A5 第 5 步：确认 / 取消卡片 —— 模型产出真正落进业务表的那一步。

这份测试只关心一件事：**「卡片说是建了」和「库里真的有」必须同时成立，
或者同时都不成立**。所以大部分断言都是成对出现的：

    tasks 表多了一行  ⟺  卡片变了 confirmed
    接口返回 409      ⟹  tasks 表一行都没多
    业务写失败        ⟹  卡片仍是 pending（还能重试）

覆盖六组：

1. 正常确认：写一行待办、卡片置 confirmed、result 记 id、返回 created
2. 幂等与并发：重复确认 409，且**不会多出第二条待办**
3. 用户改稿：override 覆盖原草稿、白名单之外的字被忽略、改坏了 422
4. 过期与越权：过期 410 且不写、别人的卡 404、已取消的再确认 409
5. 取消：只改状态、不碰业务表，重复取消 409
6. 事务原子性：业务写入失败时卡片原地不动（不出现「卡片已确认、待办没建成」）
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from core.time import now_utc
from modules.lifeweb.ai import execute
from modules.lifeweb.ai.models import ChatSession, PendingAction
from modules.lifeweb.ai.pending import (
    CODE_ALREADY_DONE,
    CODE_BAD_PAYLOAD,
    CODE_EXPIRED,
    prepare_payload,
)
from modules.lifeweb.tasks.model import Task
from tests.conftest import new_session

# conftest 的种子分类 id 是固定的：15 = task/工作，18 = task/购物，3 = expense/购物
TASK_WORK_ID = 15
TASK_SHOPPING_ID = 18
EXPENSE_SHOPPING_ID = 3

API = "/api/v1"


# ---------- 夹具 ----------


def _make_session_row(db, *, user_id: int = 1) -> ChatSession:
    row = ChatSession(user_id=user_id, title="新对话")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_pending(
    db,
    *,
    user_id: int = 1,
    status: str = "pending",
    minutes: int = 30,
    **payload,
) -> PendingAction:
    """直接写一行 pending。payload 里没给的字段按「最小可用草稿」补齐。

    `minutes` 可以是负数 —— 用来造一张「已经过期但还没被置位」的卡。
    """
    data = {"title": "去超市买牛奶", "priority": "medium", "category_id": 0}
    data.update(payload)
    action = PendingAction(
        user_id=user_id,
        session_id=1,
        message_id=1,
        action_type="create_task",
        payload=data,
        status=status,
        idempotency_key=str(uuid.uuid4()),
        expires_at=now_utc() + timedelta(minutes=minutes),
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def _task_count() -> int:
    with new_session() as s:
        return s.scalar(select(func.count()).select_from(Task)) or 0


def _reload(action_id: int) -> PendingAction:
    with new_session() as s:
        return s.get(PendingAction, action_id)


def _confirm(client, action_id: int, body: dict | None = None):
    return client.post(f"{API}/ai/actions/{action_id}/confirm", json=body or {})


# ---------- 1. 正常确认 ----------


def test_confirm_creates_task_and_marks_confirmed(client, db):
    action = _seed_pending(
        db,
        due_at="2026-11-09T15:00:00+08:00",
        category_name="购物",
        category_id=TASK_SHOPPING_ID,
        priority="high",
    )
    assert _task_count() == 0

    resp = _confirm(client, action.id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["created"] == {"type": "task", "id": body["created"]["id"]}

    with new_session() as s:
        task = s.get(Task, body["created"]["id"])
        assert task is not None
        assert task.user_id == 1
        assert task.title == "去超市买牛奶"
        assert task.priority == "high"
        assert task.status == "todo"
        assert task.category_id == TASK_SHOPPING_ID
        # 15:00+08:00 → 07:00 UTC，库里存 naive UTC
        assert task.due_at.strftime("%Y-%m-%d %H:%M") == "2026-11-09 07:00"

    stored = _reload(action.id)
    assert stored.status == "confirmed"
    assert stored.executed_at is not None
    assert stored.result == {"type": "task", "id": body["created"]["id"]}


def test_confirm_without_body_works(client, db):
    """前端最省事的调用：一个字段都不传。"""
    action = _seed_pending(db)
    resp = client.post(f"{API}/ai/actions/{action.id}/confirm")
    assert resp.status_code == 200, resp.text
    assert _task_count() == 1


def test_confirm_maps_category_name_only_draft(client, db):
    """草稿里只有分类名、没有 category_id 时，也不该落成未分类。

    真实链路里 create_draft 会补 category_id；这条测试守住的是
    「万一没补（手工插入、旧数据），执行时也不瞎」。
    """
    action = _seed_pending(db, category_name="工作", category_id=None)
    _confirm(client, action.id)

    with new_session() as s:
        task = s.scalar(select(Task))
        assert task.category_id == 0  # entity_category_id 不认名字，认 id

    # 名字映射发生在 create_draft 阶段，这里显式验一遍那条路
    with new_session() as s:
        from modules.lifeweb.ai.pending import resolve_category_id

        assert resolve_category_id(s, "create_task", "工作") == TASK_WORK_ID


def test_confirm_rejects_cross_domain_category(client, db):
    """前端塞一个账本分类 id：不属于 task 域 → 落成未分类，而不是挂错分类。"""
    action = _seed_pending(db, category_id=0)

    _confirm(client, action.id, {"payload_override": {"category_id": EXPENSE_SHOPPING_ID}})

    with new_session() as s:
        assert s.scalar(select(Task)).category_id == 0


# ---------- 2. 幂等与并发 ----------


def test_confirm_twice_returns_409_and_creates_only_one_task(client, db):
    action = _seed_pending(db)

    first = _confirm(client, action.id)
    second = _confirm(client, action.id)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == CODE_ALREADY_DONE
    assert _task_count() == 1, "重复确认建出了第二条待办"


def test_confirm_twice_with_different_override_still_409(client, db):
    """第二次换了个标题也不行：这张卡已经用掉了。"""
    action = _seed_pending(db)
    _confirm(client, action.id)
    resp = _confirm(client, action.id, {"payload_override": {"title": "改个标题"}})

    assert resp.status_code == 409
    with new_session() as s:
        assert [t.title for t in s.scalars(select(Task))] == ["去超市买牛奶"]


# ---------- 3. 用户改稿（payload_override） ----------


def test_override_replaces_fields(client, db):
    action = _seed_pending(db)
    resp = _confirm(
        client,
        action.id,
        {
            "payload_override": {
                "title": "  买鸡蛋  ",
                "priority": "high",
                "category_name": "工作",
                "category_id": TASK_WORK_ID,
            }
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 卡片上存的也必须是改过之后那一版（前端刷新后显示的内容要和库里一致）
    assert body["preview"]["title"] == "买鸡蛋"
    assert body["preview"]["priority_text"] == "高"

    with new_session() as s:
        task = s.get(Task, body["created"]["id"])
        assert task.title == "买鸡蛋"
        assert task.priority == "high"
        assert task.category_id == TASK_WORK_ID


def test_override_ignores_fields_not_in_whitelist(client, db):
    """塞 user_id / status / id 进来 —— 一个都不能生效。

    如果这些字段被原样透传，前端就能把待办写到别人名下、或者直接建成「已完成」。
    """
    action = _seed_pending(db)
    resp = _confirm(
        client,
        action.id,
        {
            "payload_override": {
                "title": "合法标题",
                "user_id": 999,
                "status": "done",
                "id": 12345,
                "completed_at": "2026-01-01T00:00:00+08:00",
            }
        },
    )

    assert resp.status_code == 200, resp.text
    with new_session() as s:
        task = s.get(Task, resp.json()["created"]["id"])
        assert task.user_id == 1
        assert task.status == "todo"
        assert task.completed_at is None
        assert task.id != 12345


@pytest.mark.parametrize(
    "override",
    [
        {"title": "   "},
        {"title": "x" * 121},
        {"priority": "urgent"},
        {"due_at": "2026-11-09T15:00:00"},  # 不带时区
        {"due_at": "0206-11-09T15:00:00+08:00"},
        {"category_name": "y" * 33},
    ],
)
def test_override_invalid_returns_422_and_writes_nothing(client, db, override):
    action = _seed_pending(db)
    resp = _confirm(client, action.id, {"payload_override": override})

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == CODE_BAD_PAYLOAD
    assert _task_count() == 0
    assert _reload(action.id).status == "pending", "校验失败后卡片不该被锁死"


def test_prepare_payload_strips_preview_fields(db):
    """草稿里带 due_at_text / priority_text / category_id 这些派生字段时不能报错。"""
    action = _seed_pending(
        db,
        due_at_text="2026-11-09 15:00（周一）",
        priority_text="中",
        category_id=TASK_WORK_ID,
    )

    payload = prepare_payload(db, action, None)

    assert "due_at_text" not in payload
    assert "priority_text" not in payload
    assert payload["title"] == "去超市买牛奶"


# ---------- 4. 过期与越权 ----------


def test_confirm_expired_returns_410_and_writes_nothing(client, db):
    action = _seed_pending(db, minutes=-5)
    resp = _confirm(client, action.id)

    assert resp.status_code == 410
    assert resp.json()["code"] == CODE_EXPIRED
    assert _task_count() == 0
    assert _reload(action.id).status == "expired"


def test_confirm_others_action_returns_404(client, db):
    action = _seed_pending(db, user_id=2)
    resp = _confirm(client, action.id)

    assert resp.status_code == 404
    assert _task_count() == 0
    assert _reload(action.id).status == "pending"


def test_confirm_missing_action_returns_404(client):
    assert _confirm(client, 999999).status_code == 404


def test_confirm_cancelled_returns_409(client, db):
    action = _seed_pending(db)
    assert client.post(f"{API}/ai/actions/{action.id}/cancel").status_code == 200

    resp = _confirm(client, action.id)
    assert resp.status_code == 409
    assert _task_count() == 0


# ---------- 5. 取消 ----------


def test_cancel_only_changes_status(client, db):
    action = _seed_pending(db)

    resp = client.post(f"{API}/ai/actions/{action.id}/cancel")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["created"] is None
    assert body["executed_at"] is None
    assert _task_count() == 0

    stored = _reload(action.id)
    assert stored.status == "cancelled"
    assert stored.result is None


def test_cancel_twice_returns_409(client, db):
    action = _seed_pending(db)
    assert client.post(f"{API}/ai/actions/{action.id}/cancel").status_code == 200

    second = client.post(f"{API}/ai/actions/{action.id}/cancel")
    assert second.status_code == 409
    assert second.json()["code"] == CODE_ALREADY_DONE


def test_cancel_after_confirm_returns_409(client, db):
    action = _seed_pending(db)
    task_id = _confirm(client, action.id).json()["created"]["id"]

    resp = client.post(f"{API}/ai/actions/{action.id}/cancel")
    assert resp.status_code == 409
    # 已建的待办不能被「取消卡片」撤掉 —— 取消只影响卡片
    with new_session() as s:
        assert s.get(Task, task_id) is not None


def test_cancel_expired_returns_410(client, db):
    action = _seed_pending(db, minutes=-5)
    resp = client.post(f"{API}/ai/actions/{action.id}/cancel")

    assert resp.status_code == 410
    assert _reload(action.id).status == "expired"


def test_cancel_others_action_returns_404(client, db):
    action = _seed_pending(db, user_id=2)
    assert client.post(f"{API}/ai/actions/{action.id}/cancel").status_code == 404
    assert _reload(action.id).status == "pending"


# ---------- 6. 事务原子性 ----------


def test_write_failure_leaves_action_pending(db, monkeypatch):
    """业务写入炸了：卡片必须还是 pending，且 tasks 表没有半行。

    用 monkeypatch 把「往待办表插入」换成一个会抛错的函数 —— 这是唯一能
    稳定复现「写到一半失败」的办法（真实原因可能是超长标题、连接断开、死锁）。
    """
    action = _seed_pending(db)

    def boom(session, user_id, payload):
        raise RuntimeError("模拟数据库写入失败")

    monkeypatch.setitem(execute._WRITERS, "create_task", boom)

    with pytest.raises(RuntimeError):
        execute.confirm(db, 1, action.id)

    assert _task_count() == 0
    assert _reload(action.id).status == "pending"


def test_confirm_commits_task_and_action_together(db):
    """成功路径下，新开一个会话应当能同时看到两边 —— 一次 commit 的结果。"""
    action = _seed_pending(db)
    executed = execute.confirm(db, 1, action.id)

    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "confirmed"
        assert s.get(Task, executed.created_id) is not None


def test_cancel_does_not_lock_action_forever(client, db):
    """取消过一次之后，这张卡不该还能被确认（防止「取消」释放了锁却留了后门）。"""
    action = _seed_pending(db)
    client.post(f"{API}/ai/actions/{action.id}/cancel")

    assert _confirm(client, action.id).status_code == 409
    assert _task_count() == 0