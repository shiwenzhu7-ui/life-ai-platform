"""把「只说不做」的补救钉成测试（A11 补丁 · 方案 B）。

真机会话 85/86/87：模型在正文里写「我整理了一条待办，请你确认」，
底下却一张卡都没有 —— 它这一轮压根没调工具。提示词改了两版都压不住
（真机 3 次里 2 次复现），只能在服务端回头问它一次：
工具声明再发一遍 + 一句系统提示，只调一次。

两条底线必须守住，否则这个补丁比 bug 更花钱：
- 只对**写入承诺**生效。用户问「我整理了哪些待办」这种正常问句不能被误判，
  否则每次查询都要多等一轮模型。
- 说「我记好了」但没有卡时，宁可回「没记下来」，也不能把假承诺留在屏幕上。
"""

import json

import pytest
from sqlalchemy import select

from conftest import API, new_session

from app import app
from modules.lifeweb.ai import service
from modules.lifeweb.ai.models import ChatMessage, PendingAction
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.tasks.model import Task


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


def _cards(message_id: int) -> list[PendingAction]:
    with new_session() as fresh:
        return list(
            fresh.query(PendingAction).filter_by(message_id=message_id).all()
        )


def test_nudge_turns_a_promise_into_a_real_card(client, db, provider):
    """第一次只说不做 → 补问一次 → 这次真调了工具 → 有卡，正文照旧。"""
    provider.queue(LLMReply(content="好，我整理了一条待办，请你确认。"))
    provider.queue(_tool_reply())

    resp = _chat(client, "提醒我买水果")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending_action"] is not None
    assert body["pending_action"]["preview"]["title"] == "买水果"
    assert "请你确认" in body["reply"]

    assert len(provider.calls) == 2
    # 补问那一轮必须带着工具声明，否则模型想调也调不了
    assert provider.calls[1]["tools"] is not None
    nudged = provider.calls[1]["messages"][-1]["content"]
    assert "没有真的调用工具" in nudged
    assert "提醒我买水果" in nudged
    # 卡片挂在补问后那条回复上：确认卡 id 能查到
    assert len(_cards(body["message_id"])) == 1


def test_nudge_gives_up_and_says_it_was_not_saved(client, db, provider):
    """补问也没用 → 正文换成事实话「没记下来」，不能留着假承诺。"""
    provider.queue(LLMReply(content="好，我整理了一条待办，请你确认。"))
    provider.queue(LLMReply(content="好的，已经帮你记好了。"))

    resp = _chat(client, "提醒我买水果")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending_action"] is None
    assert body["reply"] == service.FALLBACK_NOT_SAVED
    assert "请你确认" not in body["reply"]

    with new_session() as fresh:
        row = fresh.get(ChatMessage, body["message_id"])
        assert row.content == service.FALLBACK_NOT_SAVED
        assert "没有真的调用工具" in (row.error_message or "")
        assert fresh.query(Task).count() == 0


def test_nudge_is_tried_only_once(client, db, provider):
    """连补问都不调的话，不许无限追 —— 最多多一次调用。"""
    provider.queue(LLMReply(content="我帮你记一条待办，请你确认。"))
    provider.queue(LLMReply(content="我帮你记一条待办，请你确认。"))
    provider.queue(LLMReply(content="我帮你记一条待办，请你确认。"))

    resp = _chat(client, "提醒我买水果")
    assert resp.status_code == 200
    assert len(provider.calls) == 2
    assert resp.json()["reply"] == service.FALLBACK_NOT_SAVED


def test_plain_chat_is_not_nudged(client, db, provider):
    """普通聊天（没有写入承诺）一次都不多调。"""
    provider.queue(LLMReply(content="累了就早点睡。"))
    resp = _chat(client, "今天好累")
    assert resp.status_code == 200
    assert len(provider.calls) == 1
    assert resp.json()["reply"] == "累了就早点睡。"


def test_query_style_answer_is_not_nudged(client, db, provider):
    """「我整理了你的三个问题」不是写入承诺，不能因为带「整理」就补问。"""
    provider.queue(LLMReply(content="我整理了一下你的三个问题，逐个说：第一……"))
    resp = _chat(client, "帮我梳理下这几个问题")
    assert resp.status_code == 200
    assert len(provider.calls) == 1


def test_ask_back_without_tool_is_not_nudged(client, db, provider):
    """反问（不给卡、也没承诺）是正常行为 —— 提示词第 4 条要求它先问清。"""
    provider.queue(LLMReply(content="什么时候提醒你？"))
    resp = _chat(client, "我想让你提醒我买水果")
    assert resp.status_code == 200
    assert len(provider.calls) == 1
    assert resp.json()["pending_action"] is None


def test_tools_can_be_switched_off_by_config(client, db, provider, monkeypatch):
    """配置 llm_tools_enabled=False 时退回纯聊天：不发 tools、也不补问。"""
    from core.config import settings

    monkeypatch.setattr(settings, "llm_tools_enabled", False)
    provider.queue(LLMReply(content="我整理了一条待办，请你确认。"))

    resp = _chat(client, "提醒我买水果")
    assert resp.status_code == 200
    assert len(provider.calls) == 1
    assert provider.calls[0]["tools"] is None
    # 提示词里也不该再讲工具规则：说了有工具却没发，模型只会假装调用
    system = provider.calls[0]["messages"][0]["content"]
    assert "你可以通过调用工具" not in system
    assert resp.json()["pending_action"] is None


def test_gibberish_request_with_empty_reply_is_nudged(client, db, provider):
    """用户明确说「帮我记个待办」，模型却只回空正文 —— 也要补问（否则屏幕一片空白）。"""
    provider.queue(LLMReply(content=""))
    provider.queue(_tool_reply("去银行"))

    resp = _chat(client, "帮我记个待办：周五下午三点去银行")
    assert resp.status_code == 200
    assert len(provider.calls) == 2
    assert resp.json()["pending_action"]["preview"]["title"] == "去银行"


def test_chitchat_mention_is_not_nudged(client, db, provider):
    """用户闲聊里提一句「我最近在看书」：模型回一段话是对的，不该被拽去建卡。"""
    provider.queue(LLMReply(content=""))
    resp = _chat(client, "我最近在看书")
    assert resp.status_code == 200
    assert len(provider.calls) == 1
    assert resp.json()["pending_action"] is None


def test_nudged_card_is_not_blocked_by_the_single_card_rule(client, db, provider):
    """已有卡时用户又明确要记一件新事：模型漏调 → 补问调出来了 → 这张新卡必须建。

    真机 4 次会话都复现过：第 5 句「帮我记个待办：周五下午三点去银行」被
    「上面那张卡还在等你确认」挡回去了 —— 那件事就此消失，用户以为说了就记了。
    """
    provider.queue(_tool_reply("买水果"))
    first = _chat(client, "帮我记个待办：买水果")
    assert first.status_code == 200
    assert first.json()["pending_action"] is not None
    session_id = first.json()["session_id"]

    # 第二件事：第一轮漏调工具，补问后才调出来
    provider.queue(LLMReply(content="好，我整理了一条待办，请你确认。"))
    provider.queue(_tool_reply("去银行"))
    resp = _chat(client, "帮我记个待办：周五下午三点去银行", session_id)
    assert resp.status_code == 200
    assert resp.json()["pending_action"] is not None
    assert resp.json()["pending_action"]["preview"]["title"] == "去银行"


def test_a_different_task_gets_its_own_card(client, db, provider):
    """模型自己调工具、要记的是**另一件事** → 新卡建出来，旧卡过期。

    真机会话 116 的反面：那时这条路径被「有卡就不建」拦住，用户明确要求的
    「周五下午三点去银行」凭空消失。现在按内容区分 —— 买水果 ≠ 去银行。
    """
    provider.queue(_tool_reply("买水果"))
    first = _chat(client, "帮我记个待办：买水果")
    assert first.status_code == 200
    session_id = first.json()["session_id"]
    old_id = first.json()["pending_action"]["id"]

    provider.queue(_tool_reply("去银行"))
    resp = _chat(client, "帮我记个待办：周五下午三点去银行", session_id)
    assert resp.status_code == 200
    card = resp.json()["pending_action"]
    assert card is not None
    assert card["preview"]["title"] == "去银行"

    # 旧卡被标 expired（留痕，不是删除），屏幕上只剩一张还能点的
    db.expire_all()
    old = db.get(PendingAction, old_id)
    assert old is not None and old.status == "expired"


def test_the_same_task_again_is_still_suppressed(client, db, provider):
    """同一件事又被发一遍 → 拦下，不建第二张（448075a 要治的重复出卡）。"""
    provider.queue(_tool_reply("买水果"))
    first = _chat(client, "帮我记个待办：买水果")
    session_id = first.json()["session_id"]

    provider.queue(_tool_reply("买水果"))
    resp = _chat(client, "提醒我买水果", session_id)
    assert resp.status_code == 200
    assert resp.json()["pending_action"] is None
    assert resp.json()["reply"] == service.FALLBACK_SUPPRESSED

    db.expire_all()
    open_now = db.scalars(
        select(PendingAction).where(
            PendingAction.session_id == session_id,
            PendingAction.status == "pending",
        )
    ).all()
    assert len(open_now) == 1


def test_model_resends_the_old_task_then_redoes_the_new_one(client, db, provider):
    """模型把旧卡又发一遍（而用户说的是另一件事）→ 补问点名「按这句话提」，这一轮救回来。

    真机会话 120 的原文：旧卡「买水果」，用户说「帮我记个待办：周五下午三点去银行」，
    模型**第二次**还是交 `create_task{title: 买水果}`。当时的实现直接回
    「上面那张卡还在等你确认」，用户要的「去银行」整句话消失。
    """
    provider.queue(_tool_reply("买水果"))
    first = _chat(client, "帮我记个待办：买水果")
    session_id = first.json()["session_id"]
    old_id = first.json()["pending_action"]["id"]

    # 第一次：模型还是发旧的那件 → 被拦 → 触发补问
    provider.queue(_tool_reply("买水果"))
    # 第二次（补问之后）：这次按用户原话说对了
    provider.queue(_tool_reply("去银行"))

    resp = _chat(client, "帮我记个待办：周五下午三点去银行", session_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending_action"] is not None
    assert body["pending_action"]["preview"]["title"] == "去银行"

    # 三次调用：原请求 / 被拦那次 / 补问之后
    assert len(provider.calls) == 3
    redone = provider.calls[2]["messages"][-1]["content"]
    assert "已经挂着的那件事" in redone
    # 用户原话要贴回去，模型才知道这次该提取哪一句
    assert "周五下午三点去银行" in redone

    db.expire_all()
    old = db.get(PendingAction, old_id)
    assert old is not None and old.status == "expired"


def test_resend_that_stays_stubborn_says_it_was_not_saved(client, db, provider):
    """补问之后还是发旧的那件事 → 别再说「上面那张卡在等你确认」，直说没记下来。"""
    provider.queue(_tool_reply("买水果"))
    session_id = _chat(client, "帮我记个待办：买水果").json()["session_id"]

    provider.queue(_tool_reply("买水果"))
    provider.queue(_tool_reply("买水果"))

    resp = _chat(client, "帮我记个待办：周五下午三点去银行", session_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["pending_action"] is None
    # 关键：不能是 FALLBACK_SUPPRESSED（那句话会让用户以为只是旧卡挡着）
    assert body["reply"] == service.FALLBACK_NOT_SAVED

    db.expire_all()
    open_now = db.scalars(
        select(PendingAction).where(
            PendingAction.session_id == session_id,
            PendingAction.status == "pending",
        )
    ).all()
    assert len(open_now) == 1  # 旧卡没被顶掉，也没多出第二张


def test_resend_of_the_same_task_is_not_retried_forever(client, db, provider):
    """用户说的就是旧卡那件事（没有新内容）→ 只拦，不补问，别浪费一次调用。"""
    provider.queue(_tool_reply("买水果"))
    first = _chat(client, "帮我记个待办：买水果")
    session_id = first.json()["session_id"]
    base = len(provider.calls)

    provider.queue(_tool_reply("买水果"))
    resp = _chat(client, "提醒我买水果", session_id)
    assert resp.status_code == 200
    assert resp.json()["reply"] == service.FALLBACK_SUPPRESSED
    # 只有这一次调用：判据认为「就是旧卡那件事」，不做重发补问
    assert len(provider.calls) - base == 1


def test_plain_chat_after_a_card_is_not_retried(client, db, provider):
    """有卡之后用户只是闲聊 → 不补问（补问只针对「又在说另一件事要记」）。"""
    provider.queue(_tool_reply("买水果"))
    session_id = _chat(client, "帮我记个待办：买水果").json()["session_id"]
    base = len(provider.calls)

    provider.queue(LLMReply(content="今天天气不错。"))
    resp = _chat(client, "今天天气怎么样", session_id)
    assert resp.status_code == 200
    assert len(provider.calls) - base == 1


def test_read_then_write_is_not_nudged(client, db, provider):
    provider.queue(
        LLMReply(
            content="我先看看。",
            tool_calls=[
                {
                    "id": "call_q",
                    "type": "function",
                    "name": "query_tasks",
                    "arguments": json.dumps({"keyword": "买菜"}, ensure_ascii=False),
                }
            ],
        )
    )
    provider.queue(_tool_reply("买菜"))

    resp = _chat(client, "把我那条买菜的待办改一下")
    assert resp.status_code == 200
    assert len(provider.calls) == 2
    assert resp.json()["pending_action"] is not None