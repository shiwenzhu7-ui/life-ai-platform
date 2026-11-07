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

from conftest import API
from sqlalchemy.orm import Session

from app import app
from core.config import settings
from core.db import engine
from core.deps import get_current_user_id
from core.time import now_utc
from modules.lifeweb.ai.chat_message import ChatMessage
from modules.lifeweb.ai.chat_session import ChatSession


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
        app.dependency_overrides.pop(get_current_user_id, None)


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
