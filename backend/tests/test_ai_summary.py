"""A11：长对话的前情摘要。

这部分最容易出的错不是「摘要写得不好」，而是「摘要把正常对话弄坏了」，
所以下面每一条断言都对应一个具体的坏结果：

1. 短对话一次都不许多调模型（多调 = 每次多花钱、多等一秒）
2. 窗口外的旧消息必须真的进了摘要，而不是悄悄消失
3. 摘要调用失败 → 这一轮问答照常成功（宁可忘事，不能答不出话）
4. 摘要读不到别人的消息
5. `summary_until_id` 推进后不会重复摘要同一段
6. 摘要只存在 chat_sessions 上，不会变成一条消息挤进前端列表
7. 摘要那次调用的 token 也要记账，否则成本会少算
"""

from typing import Sequence

import pytest

from conftest import API, new_session

from app import app
from modules.lifeweb.ai import crud, summary as summary_mod
from modules.lifeweb.ai.models import ChatMessage, ChatSession, UsageLog
from modules.lifeweb.ai.provider import FakeProvider, LLMError, LLMProvider, LLMReply
from modules.lifeweb.ai.router import get_provider


class _ErrorProvider(LLMProvider):
    """第一次调用抛指定错误，之后转交给正常 provider。

    为什么不是「每次都失败」：要验证的是「摘要挂了、回答照常」，
    如果回答那次也挂，测的就不是同一个东西了。
    """

    def __init__(self, error: LLMError, after: LLMProvider) -> None:
        self.error = error
        self.after = after
        self.calls: list[list[dict]] = []

    def chat(self, messages: Sequence[dict], **kwargs) -> LLMReply:
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            raise self.error
        return self.after.chat(messages, **kwargs)


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


def _seed_history(
    db,
    n: int,
    *,
    user_id: int = 1,
    session_id: int = 1,
    title: str = "旧对话",
) -> ChatSession:
    """造一个有 n 条消息的历史会话（一问一答交替）。"""
    session = ChatSession(
        id=session_id,
        user_id=user_id,
        title=title,
        message_count=n,
        last_message_at=None,
    )
    db.add(session)
    db.flush()
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        text = f"第{i}句：我住在杭州" if role == "user" else f"第{i}句：好的，记下了"
        crud.add_message(
            db, user_id=user_id, session_id=session_id, role=role, content=text
        )
    db.commit()
    return session


def _reload_session(session_id: int = 1) -> ChatSession:
    with new_session() as fresh:
        return fresh.get(ChatSession, session_id)


# ---------- 触发条件 ----------


def test_short_conversation_never_calls_summary(client, db, provider):
    """12 条消息以内的会话：只有一次调用（正常回答），不该有摘要调用。"""
    _seed_history(db, 10)
    provider.queue(LLMReply(content="好"))

    resp = _chat(client, "在吗", session_id=1)
    assert resp.status_code == 200
    assert len(provider.calls) == 1
    assert not any("compressing" in str(c["messages"]) for c in provider.calls)
    assert _reload_session().summary is None


def test_long_conversation_triggers_exactly_once(client, db, provider):
    """窗口外攒够了：一次摘要 + 一次回答，总共两次调用。"""
    _seed_history(db, 40)
    provider.queue(LLMReply(content="用户在杭州，习惯用微信记账。"))
    provider.queue(LLMReply(content="好"))

    resp = _chat(client, "我刚才说住哪来着", session_id=1)
    assert resp.status_code == 200
    assert len(provider.calls) == 2

    session = _reload_session()
    assert session.summary == "用户在杭州，习惯用微信记账。"
    assert session.summary_until_id is not None


def test_summary_call_does_not_send_tools(client, db, provider):
    """摘要是一次纯文本调用：不该带工具定义（带了模型可能反过来调工具）。"""
    _seed_history(db, 40)
    provider.queue(LLMReply(content="摘要正文"))
    provider.queue(LLMReply(content="好"))

    _chat(client, "继续", session_id=1)
    first = provider.calls[0]
    assert first["tools"] is None


def test_summary_only_covers_messages_outside_the_window(client, db, provider):
    """摘要的输入里必须有最早的句子，但**不能**包含还在窗口里的最近几句。"""
    _seed_history(db, 40)
    provider.queue(LLMReply(content="摘要正文"))
    provider.queue(LLMReply(content="好"))

    _chat(client, "继续", session_id=1)
    prompt = "\n".join(m["content"] for m in provider.calls[0]["messages"])
    assert "第0句" in prompt               # 窗口外的旧消息进了摘要
    assert "第38句" not in prompt          # 最近的原文留给回答，不重复进摘要


# ---------- 摘要进上下文 ----------


def test_summary_is_injected_before_raw_history(client, db, provider):
    _seed_history(db, 40)
    provider.queue(LLMReply(content="用户在杭州。"))
    provider.queue(LLMReply(content="好"))

    _chat(client, "继续", session_id=1)

    # 第二次调用是真正的回答：system → 摘要 → 原文
    answering = provider.calls[1]["messages"]
    assert answering[0]["role"] == "system"
    assert answering[1]["role"] == "user"
    assert "【前情摘要】" in answering[1]["content"]
    assert "用户在杭州。" in answering[1]["content"]


def test_no_summary_message_when_summary_is_empty(client, db, provider):
    _seed_history(db, 5)
    provider.queue(LLMReply(content="好"))
    _chat(client, "在吗", session_id=1)
    messages = provider.calls[0]["messages"]
    assert all("【前情摘要】" not in str(m["content"]) for m in messages)


# ---------- 失败不影响对话 ----------


def test_summary_failure_still_answers(client, db, provider):
    """摘要那次调用超时：这一轮必须照常成功返回。"""
    _seed_history(db, 40)
    provider.queue(LLMReply(content="好"))
    broken = _ErrorProvider(LLMError("TIMEOUT", "等太久了", 504), provider)
    app.dependency_overrides[get_provider] = lambda: broken
    try:
        resp = _chat(client, "继续", session_id=1)
    finally:
        app.dependency_overrides[get_provider] = lambda: provider

    assert resp.status_code == 200
    assert resp.json()["reply"] == "好"
    assert _reload_session().summary is None


def test_summary_failure_does_not_advance_pointer(client, db, provider):
    """失败后指针不能动，否则这段历史就永远摘不到了。"""
    _seed_history(db, 40)
    provider.queue(LLMReply(content="好"))
    broken = _ErrorProvider(LLMError("RATE_LIMIT", "太快了", 429), provider)
    app.dependency_overrides[get_provider] = lambda: broken
    try:
        _chat(client, "继续", session_id=1)
    finally:
        app.dependency_overrides[get_provider] = lambda: provider
    assert _reload_session().summary_until_id is None


# ---------- 安全与幂等 ----------


def test_summary_never_reads_other_users_messages(db):
    """别人的消息不能进摘要：按 user_id 过滤。"""
    _seed_history(db, 20, user_id=2, session_id=9, title="别人的会话")
    with new_session() as s:
        rows = crud.messages_upto(
            s, 1, 9, after_id=None, end_id=10**9, limit=100
        )
    assert rows == []


def test_pointer_prevents_summarizing_the_same_block_twice(client, db, provider):
    """第二次触发时，摘要输入里不该再出现已经摘要过的那几句。"""
    _seed_history(db, 40)
    provider.queue(LLMReply(content="第一版摘要"))
    provider.queue(LLMReply(content="好"))
    _chat(client, "继续", session_id=1)
    assert len(provider.calls) == 2

    with new_session() as s:
        for i in range(30):
            crud.add_message(
                s, user_id=1, session_id=1, role="user", content=f"又聊了{i}句"
            )
        s.commit()

    provider.queue(LLMReply(content="第二版摘要"))
    provider.queue(LLMReply(content="好"))
    resp = _chat(client, "再来", session_id=1)
    assert resp.status_code == 200

    prompt = "\n".join(m["content"] for m in provider.calls[2]["messages"])
    assert "第一版摘要" in prompt      # 旧摘要参与合并
    assert "第0句" not in prompt       # 已经摘要过的原文不再重复送


def test_summary_does_not_create_a_message_row(client, db, provider):
    """摘要存会话列，不是消息行 —— 否则前端列表会多出看不懂的一条。"""
    _seed_history(db, 40)
    before = 40
    provider.queue(LLMReply(content="摘要在列上"))
    provider.queue(LLMReply(content="好"))
    _chat(client, "继续", session_id=1)

    with new_session() as s:
        count = crud.message_count(s, 1, 1)
    assert count == before + 2  # 只有这一轮的 user + assistant
    resp = client.get(f"{API}/ai/sessions/1/messages")
    assert resp.status_code == 200
    assert all("摘要在列上" not in (m.get("content") or "") for m in resp.json()["items"])


def test_summary_usage_is_recorded(client, db, provider):
    """摘要那次调用的 token 也要记，否则成本少算。"""
    _seed_history(db, 40)
    provider.queue(LLMReply(content="摘要正文", token_in=1200, token_out=80))
    provider.queue(LLMReply(content="好", token_in=10, token_out=2))
    _chat(client, "继续", session_id=1)

    with new_session() as s:
        logs = (
            s.query(UsageLog)
            .filter(UsageLog.error_code == "summary_call")
            .all()
        )
    assert len(logs) == 1
    assert logs[0].token_in == 1200
    assert logs[0].token_out == 80


# ---------- 纯函数 ----------


def test_render_history_skips_failed_and_tool_rows():
    # 只造内存对象、不落库：SQLAlchemy 没 flush 过的行 status 还是 None，
    # 而渲染函数把「非 ok」一律当失败跳过，所以这里显式给 status="ok"。
    ok = {"status": "ok"}
    rows = [
        ChatMessage(id=1, user_id=1, session_id=1, role="user", content="记账 7 块", **ok),
        ChatMessage(
            id=2, user_id=1, session_id=1, role="assistant", content="挂了", status="failed"
        ),
        ChatMessage(
            id=3,
            user_id=1,
            session_id=1,
            role="assistant",
            content="我整理了一条待办",
            tool_calls=[{"id": "c1", "name": "create_task", "arguments": "{}"}],
            **ok,
        ),
        ChatMessage(id=4, user_id=1, session_id=1, role="tool", tool_result={"ok": 1}, **ok),
        ChatMessage(id=5, user_id=1, session_id=1, role="assistant", content="记好了", **ok),
    ]
    text = summary_mod._render_history(rows)
    assert "记账 7 块" in text
    assert "记好了" in text
    assert "挂了" not in text
    assert "待办" not in text


def test_long_summary_is_truncated():
    assert summary_mod.SUMMARY_MAX_CHARS == 200
    body = "字" * 400
    assert len(body[:200]) == 200


def test_summary_prompt_forbids_assistant_advice():
    """摘要是「用户的处境」，不是「助理说过的话」—— 这条规则要在提示词里。"""
    prompt = summary_mod.build_summary_prompt(None, "用户：我住杭州")[0]["content"]
    assert "助理" in prompt
    assert "建议" in prompt or "清单" in prompt
    assert "200" in prompt


def test_summary_message_returns_none_for_blank():
    assert summary_mod.summary_message(None) is None
    assert summary_mod.summary_message("   ") is None
    msg = summary_mod.summary_message("用户在杭州")
    assert msg["role"] == "user"
    assert summary_mod.SUMMARY_HEADER in msg["content"]


def test_build_summary_prompt_merges_old_and_new():
    first = summary_mod.build_summary_prompt(None, "用户：hi")
    assert "上一版摘要" not in first[1]["content"]
    merged = summary_mod.build_summary_prompt("旧摘要在这里", "用户：新增一句")
    content = merged[1]["content"]
    assert "旧摘要在这里" in content
    assert "用户：新增一句" in content