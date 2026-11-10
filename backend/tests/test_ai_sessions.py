"""AI 会话接口（A3）：会话 CRUD + 消息读取。

这一层没有模型调用，所以测试全是确定性的 —— 不需要 FakeProvider。

四个必须守住的点：
- user_id 只认服务端注入，请求体里塞了也不算数（否则能往别人账户写会话）
- 别人的会话统一 404，不是 403（403 等于确认「这个 ID 存在」）
- 软删只写 deleted_at：列表看不见，但行还在（用量统计还要用）
- 消息只读：写消息是 A4 的事，这里不该出现 POST
"""

from contextlib import contextmanager
from datetime import timedelta
import uuid

from conftest import API
from sqlalchemy.orm import Session

from app import app
from core.config import settings
from core.db import engine
from core.deps import get_current_user_id
from core.time import now_utc
from modules.lifeweb.ai.models import ChatMessage, ChatSession, PendingAction


@contextmanager
def _acting_as(user_id: int):
    """临时把「当前用户」换成另一个人。

    V1.0 的 get_current_user_id 写死返回 1，只能用依赖覆盖来模拟第二个用户；
    V1.2 接了登录以后，这里有更真实的隔离手段可以替换。
    """
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    try:
        yield
    finally:
        # 退出时必须还原成 1 号而不是直接删掉：conftest 的 _clean_db 每个用例只
        # 设一次覆盖，这里 pop 掉的话，同一用例里后续的请求就会真的去解 JWT，
        # 变成 401（没有 Authorization 头）。
        app.dependency_overrides[get_current_user_id] = lambda: 1


def _make_session(
    db: Session,
    *,
    user_id: int = 1,
    title: str = "新对话",
    last_message_at=None,
    deleted_at=None,
) -> ChatSession:
    session = ChatSession(
        user_id=user_id,
        title=title,
        last_message_at=last_message_at,
        deleted_at=deleted_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_message(
    db: Session,
    session_id: int,
    *,
    user_id: int = 1,
    role: str = "user",
    content: str = "你好",
) -> ChatMessage:
    message = ChatMessage(user_id=user_id, session_id=session_id, role=role, content=content)
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


# ---------- 新建 ----------


def test_create_session_without_body_uses_defaults(client):
    res = client.post(f"{API}/ai/sessions")

    assert res.status_code == 201
    body = res.json()
    assert body["id"] > 0
    assert body["title"] == "新对话"
    assert body["model"] == settings.llm_model
    assert body["message_count"] == 0
    assert body["last_message_at"] is None
    assert body["created_at"].endswith("+08:00")


def test_create_session_strips_title(client):
    body = client.post(f"{API}/ai/sessions", json={"title": "  周末计划  "}).json()

    assert body["title"] == "周末计划"


def test_create_session_rejects_blank_or_too_long_title(client):
    for bad in ("", "   ", "x" * 121):
        res = client.post(f"{API}/ai/sessions", json={"title": bad})

        assert res.status_code == 422, bad


def test_create_session_ignores_client_supplied_user_id(client):
    """请求体里塞 user_id 也不能改归属 —— 这是防越权写入的第二道验证。"""
    res = client.post(f"{API}/ai/sessions", json={"title": "我的", "user_id": 999})

    assert res.status_code == 201
    assert [item["title"] for item in client.get(f"{API}/ai/sessions").json()["items"]] == ["我的"]


# ---------- 列表 ----------


def test_list_sessions_orders_by_last_message_desc_and_empty_last(client, db):
    old = _make_session(
        db, title="两小时前", last_message_at=now_utc() - timedelta(hours=2)
    )
    fresh = _make_session(db, title="刚刚", last_message_at=now_utc())
    empty = _make_session(db, title="空会话")

    body = client.get(f"{API}/ai/sessions").json()

    assert [item["id"] for item in body["items"]] == [fresh.id, old.id, empty.id]
    assert body["total"] == 3
    assert body["pages"] == 1


def test_list_sessions_paginates(client, db):
    for i in range(3):
        _make_session(db, title=f"会话{i}")

    first = client.get(f"{API}/ai/sessions", params={"page": 1, "size": 2}).json()
    second = client.get(f"{API}/ai/sessions", params={"page": 2, "size": 2}).json()

    assert first["total"] == 3
    assert first["pages"] == 2
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1


def test_list_sessions_excludes_soft_deleted(client, db):
    alive = _make_session(db, title="还在")
    _make_session(db, title="已删", deleted_at=now_utc())

    body = client.get(f"{API}/ai/sessions").json()

    assert [item["id"] for item in body["items"]] == [alive.id]
    assert body["total"] == 1


# ---------- 详情（A13） ----------


def test_get_session_returns_the_same_shape_as_list(client, db):
    """详情与列表同一条序列化路径：前端拿到两份不会对不上。"""
    session = _make_session(db, title="买水果", last_message_at=now_utc())
    session.message_count = 1
    db.commit()
    _make_message(db, session.id, role="assistant", content="我整理了一条待办，请你确认。")

    detail = client.get(f"{API}/ai/sessions/{session.id}")
    listed = client.get(f"{API}/ai/sessions").json()["items"][0]

    assert detail.status_code == 200
    assert detail.json() == listed
    assert detail.json()["title"] == "买水果"
    assert detail.json()["message_count"] == 1


def test_get_session_of_empty_session(client, db):
    session = _make_session(db)

    body = client.get(f"{API}/ai/sessions/{session.id}").json()

    assert body["id"] == session.id
    assert body["message_count"] == 0
    assert body["last_message_at"] is None


def test_get_session_missing_or_deleted_or_not_mine_is_404(client, db):
    """三种「不能看」走同一个 404：不泄露这个 id 到底存不存在。"""
    alive = _make_session(db)
    gone = _make_session(db, deleted_at=now_utc())
    others = _make_session(db, user_id=2, title="别人的会话")

    assert client.get(f"{API}/ai/sessions/999999").status_code == 404
    assert client.get(f"{API}/ai/sessions/{gone.id}").status_code == 404
    assert client.get(f"{API}/ai/sessions/{others.id}").status_code == 404

    with _acting_as(2):
        assert client.get(f"{API}/ai/sessions/{others.id}").status_code == 200
        assert client.get(f"{API}/ai/sessions/{alive.id}").status_code == 404


def test_get_session_follows_rename(client, db):
    """列表那份可能是旧的，详情必须是现在库里的样子。"""
    session = _make_session(db, title="旧标题")
    client.patch(f"{API}/ai/sessions/{session.id}", json={"title": "新标题"})

    assert client.get(f"{API}/ai/sessions/{session.id}").json()["title"] == "新标题"


# ---------- 用户隔离 ----------


def test_other_users_session_is_invisible_everywhere(client, db):
    mine = _make_session(db, user_id=1, title="我的会话")
    _make_message(db, mine.id)

    with _acting_as(2):
        assert client.get(f"{API}/ai/sessions").json()["total"] == 0
        assert client.get(f"{API}/ai/sessions/{mine.id}/messages").status_code == 404
        assert client.patch(f"{API}/ai/sessions/{mine.id}", json={"title": "偷改"}).status_code == 404
        assert client.delete(f"{API}/ai/sessions/{mine.id}").status_code == 404

    # 对方操作失败后，原主人这边一点没变
    body = client.get(f"{API}/ai/sessions").json()
    assert [item["title"] for item in body["items"]] == ["我的会话"]


def test_other_users_messages_are_not_readable_even_with_same_session_id(client, db):
    """消息表自己带 user_id：即使 session_id 撞上了，也只读自己的那几行。"""
    mine = _make_session(db, user_id=1)
    _make_message(db, mine.id, user_id=1, content="我的消息")
    _make_message(db, mine.id, user_id=2, content="别人的消息")

    body = client.get(f"{API}/ai/sessions/{mine.id}/messages").json()

    assert body["total"] == 1
    assert body["items"][0]["content"] == "我的消息"


# ---------- 消息 ----------


def test_messages_of_empty_session(client, db):
    session = _make_session(db)

    body = client.get(f"{API}/ai/sessions/{session.id}/messages").json()

    assert body["items"] == []
    assert body["total"] == 0
    assert body["pages"] == 0
    assert body["size"] == 50


def test_messages_are_ordered_by_id_and_paginated(client, db):
    session = _make_session(db)
    for role, content in (("user", "第一句"), ("assistant", "第二句"), ("user", "第三句")):
        _make_message(db, session.id, role=role, content=content)

    first = client.get(
        f"{API}/ai/sessions/{session.id}/messages", params={"page": 1, "size": 2}
    ).json()
    second = client.get(
        f"{API}/ai/sessions/{session.id}/messages", params={"page": 2, "size": 2}
    ).json()

    assert [item["content"] for item in first["items"]] == ["第一句", "第二句"]
    assert [item["role"] for item in first["items"]] == ["user", "assistant"]
    assert first["total"] == 3
    assert first["pages"] == 2
    assert [item["content"] for item in second["items"]] == ["第三句"]


def test_messages_order_desc_returns_newest_first(client, db):
    """聊天窗口用 desc：第一页就该是最新几条，而不是三个月前的开场白。"""
    session = _make_session(db)
    for content in ("第一句", "第二句", "第三句"):
        _make_message(db, session.id, role="user", content=content)

    first = client.get(
        f"{API}/ai/sessions/{session.id}/messages",
        params={"page": 1, "size": 2, "order": "desc"},
    ).json()
    second = client.get(
        f"{API}/ai/sessions/{session.id}/messages",
        params={"page": 2, "size": 2, "order": "desc"},
    ).json()

    assert [item["content"] for item in first["items"]] == ["第三句", "第二句"]
    assert first["total"] == 3
    assert first["pages"] == 2
    assert [item["content"] for item in second["items"]] == ["第一句"]


def test_messages_rejects_unknown_order(client, db):
    session = _make_session(db)

    res = client.get(
        f"{API}/ai/sessions/{session.id}/messages", params={"order": "sideways"}
    )

    assert res.status_code == 422


def test_messages_of_missing_session_404(client):
    assert client.get(f"{API}/ai/sessions/999999/messages").status_code == 404


# ---------- 改标题 ----------


def test_patch_title(client, db):
    session = _make_session(db, title="旧标题")

    res = client.patch(f"{API}/ai/sessions/{session.id}", json={"title": " 新标题 "})

    assert res.status_code == 200
    assert res.json()["title"] == "新标题"


def test_patch_rejects_blank_title(client, db):
    session = _make_session(db)

    for bad in ("", "   "):
        assert client.patch(f"{API}/ai/sessions/{session.id}", json={"title": bad}).status_code == 422


def test_patch_rejects_empty_body(client, db):
    session = _make_session(db)

    assert client.patch(f"{API}/ai/sessions/{session.id}", json={}).status_code == 422


def test_patch_missing_session_404(client):
    assert client.patch(f"{API}/ai/sessions/999999", json={"title": "x"}).status_code == 404


# ---------- 删除 ----------


def test_delete_is_soft(client, db):
    session = _make_session(db)

    res = client.delete(f"{API}/ai/sessions/{session.id}")

    assert res.status_code == 204
    assert res.content == b""

    # 列表里没了
    assert client.get(f"{API}/ai/sessions").json()["total"] == 0
    # 消息也读不到（因为会话已删）
    assert client.get(f"{API}/ai/sessions/{session.id}/messages").status_code == 404
    # 但行还在，deleted_at 有值 —— 用独立会话读，避开 REPEATABLE READ 的旧快照
    with Session(engine) as fresh:
        row = fresh.get(ChatSession, session.id)
        assert row is not None
        assert row.deleted_at is not None


def test_delete_twice_returns_404(client, db):
    session = _make_session(db)

    assert client.delete(f"{API}/ai/sessions/{session.id}").status_code == 204
    assert client.delete(f"{API}/ai/sessions/{session.id}").status_code == 404


def test_delete_missing_session_404(client):
    assert client.delete(f"{API}/ai/sessions/999999").status_code == 404


# ---------- A5 第 6 步：消息列表带上确认卡片 ----------


def _make_pending(
    db: Session,
    *,
    session_id: int,
    message_id: int,
    user_id: int = 1,
    status: str = "pending",
    minutes: int = 30,
    result: dict | None = None,
) -> PendingAction:
    """造一张卡。`minutes` 可以是负数，用来造「已经过期但还没被置位」的行。"""
    action = PendingAction(
        user_id=user_id,
        session_id=session_id,
        message_id=message_id,
        action_type="create_task",
        payload={
            "title": "去超市买牛奶",
            "priority": "medium",
            "priority_text": "中",
            "category_id": 18,
            "category_name": "购物",
            "due_at_text": "2026-11-14 15:00（周六）",
        },
        status=status,
        idempotency_key=str(uuid.uuid4()),
        expires_at=now_utc() + timedelta(minutes=minutes),
        result=result,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def test_messages_carry_pending_action(client, db):
    """刷新页面的关键：草稿还在库里，界面就得能把卡片原样画出来。

    没有这个字段的话，用户关掉页面再打开，卡片会凭空消失 ——
    待办没建成，用户也不知道自己还有一张没点的卡。
    """
    session = _make_session(db)
    question = _make_message(db, session.id, role="user", content="提醒我买牛奶")
    answer = _make_message(db, session.id, role="assistant", content="我整理了一条待办，请你确认。")
    card = _make_pending(db, session_id=session.id, message_id=answer.id)

    items = client.get(f"{API}/ai/sessions/{session.id}/messages").json()["items"]
    by_id = {item["id"]: item for item in items}

    # 用户那条消息没有卡
    assert by_id[question.id]["pending_action"] is None

    pending = by_id[answer.id]["pending_action"]
    assert pending["id"] == card.id
    assert pending["action_type"] == "create_task"
    assert pending["status"] == "pending"
    # 带 +08:00 的 ISO 字符串，和 ChatResponse 里那张卡同一个格式
    assert pending["expires_at"].endswith("+08:00")
    # preview 原样透出，含可读文本 —— 前端直接显示，不再自己算时区
    assert pending["preview"]["title"] == "去超市买牛奶"
    assert pending["preview"]["due_at_text"] == "2026-11-14 15:00（周六）"
    assert pending["preview"]["category_name"] == "购物"


def test_messages_carry_resolved_cards_without_touching_status(client, db):
    """已确认 / 已取消 / 已过期的卡也要带回来，且**读列表不改状态**。

    前半句：卡片该显示成「已创建」，而不是消失或变成可点的按钮。
    后半句：列表是读路径，不能因为「翻开历史」就顺手写库 ——
    过期的行照实返回 status=pending，用户真去点时由
    pending.get_pending 置 expired 并回 410。
    """
    session = _make_session(db)
    confirmed_msg = _make_message(db, session.id, role="assistant", content="已创建")
    expired_msg = _make_message(db, session.id, role="assistant", content="这张已经放凉了")

    confirmed = _make_pending(
        db, session_id=session.id, message_id=confirmed_msg.id, status="confirmed"
    )
    stale = _make_pending(
        db, session_id=session.id, message_id=expired_msg.id, minutes=-1
    )

    items = client.get(f"{API}/ai/sessions/{session.id}/messages").json()["items"]
    by_id = {item["id"]: item for item in items}

    assert by_id[confirmed_msg.id]["pending_action"]["status"] == "confirmed"
    # 过期行没被列表接口偷偷改掉
    assert by_id[expired_msg.id]["pending_action"]["status"] == "pending"

    with Session(engine) as fresh:
        assert fresh.get(PendingAction, confirmed.id).status == "confirmed"
        assert fresh.get(PendingAction, stale.id).status == "pending"


def test_messages_expose_created_id_for_confirmed_cards(client, db):
    """确认过的那张卡，列表里要带出它建出来的记录 id。

    不带的话，「查看待办 ▸」只在确认那一次响应里活着：
    用户点完看到了，一刷新 / 一换会话就没了 —— 而记录其实一直在库里。
    数据来源是 `ai_pending_actions.result`（确认时写进去的 {"type","id"}），
    不重新查业务表，也不在主查询里加 JOIN。
    """
    session = _make_session(db)
    confirmed_msg = _make_message(db, session.id, role="assistant", content="已创建")
    pending_msg = _make_message(db, session.id, role="assistant", content="还在等他点")

    _make_pending(
        db,
        session_id=session.id,
        message_id=confirmed_msg.id,
        status="confirmed",
        result={"type": "task", "id": 151},
    )
    _make_pending(db, session_id=session.id, message_id=pending_msg.id)

    items = client.get(f"{API}/ai/sessions/{session.id}/messages").json()["items"]
    by_id = {item["id"]: item for item in items}

    assert by_id[confirmed_msg.id]["pending_action"]["created_id"] == 151
    # 还没确认的卡没有记录可链
    assert by_id[pending_msg.id]["pending_action"]["created_id"] is None


def test_created_id_survives_a_weird_result(client, db):
    """result 被手工改坏（或来自旧版本）时，卡片降级成「没链接」而不是让列表 500。"""
    session = _make_session(db)
    answer = _make_message(db, session.id, role="assistant", content="坏的")
    _make_pending(
        db,
        session_id=session.id,
        message_id=answer.id,
        status="confirmed",
        result={"type": "task", "id": "151"},
    )

    items = client.get(f"{API}/ai/sessions/{session.id}/messages").json()["items"]
    assert items[0]["pending_action"]["created_id"] is None


def test_confirm_response_carries_created_id_too(client, db):
    """确认接口的响应也要有拍平的 created_id，前端用它一次更新卡片，不必再拉列表。"""
    session = _make_session(db)
    answer = _make_message(db, session.id, role="assistant", content="请你确认")
    card = _make_pending(db, session_id=session.id, message_id=answer.id)

    resp = client.post(f"{API}/ai/actions/{card.id}/confirm")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["created"]["id"] == body["created_id"]
    """别人的卡不能挂在别人的消息上跟着返回（依赖消息本身已按 user_id 过滤）。"""
    session = _make_session(db)
    answer = _make_message(db, session.id, role="assistant", content="别人的卡")

    with _acting_as(2):
        _make_pending(db, session_id=session.id, message_id=answer.id, user_id=2)

    # 用账号 2 的身份读：会话 1 的消息一律 404，不走「空列表」这条路
    with _acting_as(2):
        assert client.get(f"{API}/ai/sessions/{session.id}/messages").status_code == 404

    # 账号 1 读自己的会话：消息在，但挂在它上面的那张卡属于账号 2，不能被带出来
    items = client.get(f"{API}/ai/sessions/{session.id}/messages").json()["items"]
    assert items[0]["content"] == "别人的卡"
    assert items[0]["pending_action"] is None
