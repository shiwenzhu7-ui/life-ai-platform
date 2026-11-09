"""把「同一会话只留一张待确认卡」钉成测试。

真机会话 82：用户说了句不相干的「今天有点累」，模型把前面那张还没确认的
「买水果」卡原样又发了一次。屏幕上两张一模一样的卡，点哪张都能用，
但用户得处理两次 —— 这类问题提示词治不住，只能在服务端挡。
"""

import json

import pytest

from conftest import API, new_session

from app import app
from modules.lifeweb.ai.models import ChatMessage, ChatSession, PendingAction
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider


@pytest.fixture()
def provider():
    fake = FakeProvider()
    app.dependency_overrides[get_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_provider, None)


def _chat(client, message: str, session_id: int | None = None):
    body = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id
    return client.post(f"{API}/ai/chat", json=body)


def _tool_reply(title: str = "买水果") -> LLMReply:
    return LLMReply(
        content="我整理了一条待办，请你确认。",
        tool_calls=[
            {
                "id": "call_create_task",
                "type": "function",
                "name": "create_task",
                "arguments": json.dumps({"title": title}, ensure_ascii=False),
            }
        ],
    )


def _cards(session_id: int) -> list[PendingAction]:
    with new_session() as fresh:
        rows = fresh.query(PendingAction).filter_by(session_id=session_id).all()
        return list(rows)


def test_second_card_in_same_session_is_suppressed(client, db, provider):
    provider.queue(_tool_reply())
    first = _chat(client, "提醒我买水果")
    assert first.status_code == 200
    session_id = first.json()["session_id"]
    assert first.json()["pending_action"] is not None
    assert len(_cards(session_id)) == 1

    provider.queue(_tool_reply())
    second = _chat(client, "今天有点累", session_id=session_id)
    assert second.status_code == 200
    assert "上面那张卡还在等你确认" in second.json()["reply"]
    assert second.json()["pending_action"] is None
    assert len(_cards(session_id)) == 1, "同一会话同时只允许一张待确认的卡"


def test_suppressed_message_does_not_say_please_confirm(client, db, provider):
    """正文不能再写「请你确认」—— 那条消息底下没有卡，用户会找不到。"""
    provider.queue(_tool_reply())
    session_id = _chat(client, "提醒我买水果").json()["session_id"]
    provider.queue(_tool_reply())
    _chat(client, "今天有点累", session_id=session_id)

    with new_session() as fresh:
        rows = (
            fresh.query(ChatMessage)
            .filter_by(session_id=session_id, role="assistant")
            .order_by(ChatMessage.id.asc())
            .all()
        )
        stored = rows[-1].content
    assert stored.endswith("再说新的事。")
    assert "请你确认" not in stored


def test_after_cancelling_the_old_card_a_new_one_is_allowed(client, db, provider):
    """旧卡被取消之后，再出卡是正常的 —— 挡的是「同时两张」，不是「一辈子一张」。"""
    provider.queue(_tool_reply())
    first = _chat(client, "提醒我买水果").json()
    session_id = first["session_id"]
    action_id = first["pending_action"]["id"]
    assert client.post(f"{API}/ai/actions/{action_id}/cancel").status_code == 200

    provider.queue(_tool_reply("买牛奶"))
    again = _chat(client, "再记一条买牛奶", session_id=session_id)
    assert again.status_code == 200
    assert again.json()["pending_action"] is not None
    assert len(_cards(session_id)) == 2


def test_expired_card_does_not_block_new_ones(client, db, provider):
    """过期卡不该挡住新卡：它已经点不动了。"""
    from datetime import timedelta

    from core.time import now_utc

    session = ChatSession(id=1, user_id=1, title="旧对话", message_count=0)
    db.add(session)
    db.flush()
    said = db.query(ChatMessage).first()
    db.add(
        PendingAction(
            user_id=1,
            session_id=1,
            message_id=1,
            action_type="create_task",
            payload={"title": "旧事"},
            status="pending",
            idempotency_key="old",
            expires_at=now_utc() - timedelta(minutes=1),
        )
    )
    db.commit()
    assert said is None

    provider.queue(_tool_reply())
    resp = _chat(client, "提醒我买水果", session_id=1)
    assert resp.status_code == 200
    assert resp.json()["pending_action"] is not None


def test_other_sessions_do_not_block_each_other(client, db, provider):
    """一个会话里挂着卡，不影响新会话建卡。"""
    provider.queue(_tool_reply())
    a = _chat(client, "提醒我买水果").json()
    assert a["pending_action"] is not None

    provider.queue(_tool_reply("买牛奶"))
    resp = _chat(client, "帮我记个待办：买牛奶")
    assert resp.status_code == 200
    b = resp.json()
    assert b["session_id"] != a["session_id"]
    assert b["pending_action"] is not None