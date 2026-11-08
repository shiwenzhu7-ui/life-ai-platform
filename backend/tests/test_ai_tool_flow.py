"""A5 第 4 步：把工具申请接进 `/ai/chat`（草稿 → `ai_pending_actions`）。

和 `test_ai_chat.py` 的区别：那边证明「对话能跑通」，这边只证明**这条新支线不越界**。
全部用 FakeProvider，不联网不花钱。六件事：

1. `tools=ALL_TOOLS` 真的传到了 provider（第 1 步的说明书终于发出去了）
2. 模型给出合格参数 → 响应里出现 `pending_action`，库里有一行 pending，
   且 `tasks` 表**一条都没多**（A5 的安全底线）
3. 模型只调工具、不说话 → `reply` 不能是空串（前端会渲染成空气泡）
4. 参数不合格 → 重试一次；重试仍不合格 → 5xx，**不落卡片、也不假装成功**
5. 重试轮模型放弃（只回文字、不带 tool_call）→ 仍然算失败，不能变成「已记好」
6. 草稿、助手消息、用量、会话计数在**同一个 commit** 里（失败就全没有）
"""

from typing import Sequence

import pytest

from conftest import API, new_session

from app import app
from modules.lifeweb.ai.models import ChatMessage, ChatSession, UsageLog
from modules.lifeweb.ai.provider import FakeProvider, LLMProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.ai.tools import ALL_TOOLS
from modules.lifeweb.tasks.model import Task

from modules.lifeweb.ai.models import PendingAction  # noqa: E402  - 放在最末尾，见同目录同名坑


def _call(name: str = "create_task", **arguments) -> dict:
    """造一个 OpenAI 形状的 tool_call。"""
    import json

    return {
        "id": f"call_{name}",
        "type": "function",
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }


def _reply(content: str | None = None, *, calls: list[dict] | None = None, **kw) -> LLMReply:
    return LLMReply(content=content, tool_calls=calls, **kw)


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


def _count(model) -> int:
    with new_session() as s:
        return s.query(model).count()


def _rows(model):
    with new_session() as s:
        return s.query(model).all()


# ---------- 正常路径 ----------


def test_chat_passes_tools_to_provider(client, provider):
    """第 1 步写的说明书必须真的发出去，否则模型永远不知道能调工具。"""
    provider.queue(_reply("随便聊聊。"))

    res = _chat(client, "你好")

    assert res.status_code == 200
    assert provider.calls[0]["tools"] == ALL_TOOLS


def test_chat_without_tool_call_returns_no_pending(client, provider):
    """绝大多数对话就是聊天：没有 tool_calls 时不重试、不落卡片。"""
    provider.queue(_reply("今天适合早点睡。"))

    res = _chat(client, "给点建议")

    body = res.json()
    assert res.status_code == 200
    assert body["pending_action"] is None
    assert body["reply"] == "今天适合早点睡。"
    assert len(provider.calls) == 1  # 只调了一次
    assert _count(PendingAction) == 0


def test_chat_with_valid_tool_call_creates_pending_action(client, provider):
    provider.queue(
        _reply(
            "我整理了一条待办，请你确认。",
            calls=[_call(title="去超市买牛奶", due_at="2026-11-09T15:00:00+08:00", category_name="购物")],
            token_in=210,
            token_out=42,
            latency_ms=1200,
            model="deepseek-chat",
        )
    )

    res = _chat(client, "明天下午三点去超市买牛奶")

    body = res.json()
    assert res.status_code == 200
    pending = body["pending_action"]
    assert pending is not None
    assert pending["action_type"] == "create_task"
    # 预览里必须有前端直接能显示的东西，而不是让前端自己算
    assert pending["preview"]["title"] == "去超市买牛奶"
    assert pending["preview"]["category_id"] == 18  # conftest 里 task/购物 = 18
    assert pending["preview"]["due_at_text"] == "2026-11-09 15:00（周一）"
    assert pending["preview"]["priority_text"] == "中"
    assert "+08:00" in pending["expires_at"]

    # 安全底线：待办表一条都没多
    assert _count(Task) == 0

    with new_session() as s:
        row = s.get(PendingAction, pending["id"])
        assert row.status == "pending"
        assert row.user_id == 1
        assert row.session_id == body["session_id"]
        assert row.message_id == body["message_id"]  # 挂在产生它的助手消息下面
        assert row.idempotency_key  # 服务端生成，非空

    # 助手消息带上了 tool_calls，方便事后追溯「这句话调了什么」
    with new_session() as s:
        msg = s.get(ChatMessage, body["message_id"])
        assert msg.status == "ok"
        assert msg.content == "我整理了一条待办，请你确认。"
        assert msg.tool_calls and msg.tool_calls[0]["name"] == "create_task"


def test_tool_only_reply_gets_fallback_text(client, provider):
    """模型只说「我调工具」、content 为空 —— 气泡不能是空白。"""
    provider.queue(_reply(None, calls=[_call(title="交房租")]))

    res = _chat(client, "提醒我月底交房租")

    body = res.json()
    assert res.status_code == 200
    assert body["reply"]  # 非空
    assert body["pending_action"]["preview"]["title"] == "交房租"


def test_unknown_category_falls_back_to_zero(client, provider):
    """模型编了个不存在的分类：卡片照样出，只是分类落 0（未分类）。"""
    provider.queue(_reply(None, calls=[_call(title="遛狗", category_name="宠物")]))

    res = _chat(client, "每天遛狗")

    preview = res.json()["pending_action"]["preview"]
    assert preview["category_id"] == 0


# ---------- 重试路径 ----------


def test_bad_arguments_are_retried_once_and_can_succeed(client, provider):
    """时间漏了时区 → 第一次被丢弃 → 把错误喂回去 → 第二次合格。"""
    provider.queue(_reply("好的。", calls=[_call(title="开会", due_at="2026-11-10 09:00")]))
    provider.queue(
        _reply("改好了。", calls=[_call(title="开会", due_at="2026-11-10T09:00:00+08:00")])
    )

    res = _chat(client, "下周一上午九点开会")

    body = res.json()
    assert res.status_code == 200
    assert len(provider.calls) == 2
    # 重试请求只带 system + 一条拼了错误说明的用户消息
    retry = provider.calls[1]
    assert retry["tools"] == ALL_TOOLS
    assert [m["role"] for m in retry["messages"]] == ["system", "user"]
    assert "没有通过校验" in retry["messages"][-1]["content"]
    assert "下周一上午九点开会" in retry["messages"][-1]["content"]

    assert body["pending_action"]["preview"]["title"] == "开会"
    assert body["reply"] == "改好了。"
    assert _count(PendingAction) == 1


def test_bad_arguments_twice_fails_the_turn(client, provider):
    """两次都错：这一轮算失败，不落卡片、不假装成功。"""
    bad = _call(title="", due_at="明天")
    provider.queue(_reply("好的。", calls=[bad]))
    provider.queue(_reply("还是好的。", calls=[bad]))

    res = _chat(client, "提醒我明天做点什么")

    assert res.status_code >= 500  # 5xx，前端提示重试
    assert _count(PendingAction) == 0
    assert _count(Task) == 0

    with new_session() as s:
        msgs = s.query(ChatMessage).order_by(ChatMessage.id).all()
        # 用户那句留着，助手的半成品行被 rollback 掉、换成一条 failed
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[1].status == "failed"
        assert msgs[1].error_message

        logs = s.query(UsageLog).all()
        assert len(logs) == 1
        assert logs[0].status == "failed"
        assert logs[0].error_code == "MALFORMED_TOOL_ARGS"


def test_retry_that_gives_up_on_tools_still_fails(client, provider):
    """重试轮模型只回文字、不再调用工具 —— 不能当成「它改主意了」。

    否则用户看到「好的，已经帮你记下了」，库里什么都没有。
    """
    provider.queue(_reply("好的。", calls=[_call(title="x" * 200)]))  # 标题超长 → 不合格
    provider.queue(_reply("好的，已经帮你记下了。"))  # 放弃重试，纯文字

    res = _chat(client, "帮我记一条待办")

    assert res.status_code >= 500
    assert _count(PendingAction) == 0

    with new_session() as s:
        msgs = s.query(ChatMessage).order_by(ChatMessage.id).all()
        assert msgs[-1].status == "failed"
        # 就算模型嘴上说「已经帮你记下了」，这行也是 failed，前端不会当成成功
        assert msgs[-1].content == "好的，已经帮你记下了。"


def test_hallucinated_tool_name_fails_without_retry(client, provider):
    """不存在的工具名不是「参数问题」：同样的说明书再发一次也改不好，不重试。"""
    provider.queue(_reply("好的。", calls=[_call("delete_all_tasks")]))
    # 不 queue 第二条：一旦重试，FakeProvider 会抛 FAKE_EXHAUSTED，测试就会以 500 变红

    res = _chat(client, "把待办全删了")

    assert res.status_code >= 500
    assert len(provider.calls) == 1  # 没有重试
    assert _count(Task) == 0
    assert _count(PendingAction) == 0


# ---------- 事务与用法 ----------


def test_pending_rows_do_not_leak_between_sessions(client, provider):
    first = client.post(f"{API}/ai/sessions", json={"title": "A"}).json()
    second = client.post(f"{API}/ai/sessions", json={"title": "B"}).json()

    provider.queue(_reply(None, calls=[_call(title="买酱油")]))
    provider.queue(_reply(None, calls=[_call(title="交网费")]))

    a = _chat(client, "买酱油", first["id"]).json()
    b = _chat(client, "交网费", second["id"]).json()

    assert a["pending_action"]["id"] != b["pending_action"]["id"]
    with new_session() as s:
        rows = {r.id: r.session_id for r in s.query(PendingAction).all()}
        assert rows[a["pending_action"]["id"]] == first["id"]
        assert rows[b["pending_action"]["id"]] == second["id"]


def test_failed_tool_turn_does_not_break_next_turn(client, provider):
    """失败那一轮不能把下一轮带崩：失败行不回灌，重试仍可正常出卡片。"""
    provider.queue(_reply("好的。", calls=[_call(title="")]))
    provider.queue(_reply("还是好的。", calls=[_call(title="")]))
    bad = _chat(client, "记一条")
    assert bad.status_code >= 500

    sid = _rows(ChatSession)[0].id
    provider.queue(_reply(None, calls=[_call(title="记一条正常待办")]))
    ok = _chat(client, "这次好好记一条", sid)

    assert ok.status_code == 200
    assert ok.json()["pending_action"]["preview"]["title"] == "记一条正常待办"
    # 上下文里没有那条 failed（失败行的 content 不进 messages）：
    # 只剩上一轮用户那句话 + 这一轮的用户提问，两条 user
    calls = provider.calls[-1]["messages"]
    assert [m["role"] for m in calls] == ["system", "user", "user"]
    assert "这次好好记一条" in calls[-1]["content"]


def test_message_count_matches_message_rows(client, provider):
    """失败和不失败都占两行，计数必须和实际行数对得上，否则前端分页会差一页。"""
    provider.queue(_reply(None, calls=[_call(title="倒垃圾")]))

    body = _chat(client, "提醒我倒垃圾").json()

    with new_session() as s:
        session = s.get(ChatSession, body["session_id"])
        rows = s.query(ChatMessage).filter_by(session_id=body["session_id"]).count()
        assert session.message_count == rows == 2