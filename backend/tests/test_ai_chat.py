"""A4：发消息 → 模型回话 → 落库（`POST /ai/chat`）。

全部用 `FakeProvider`，不联网、不花钱。这里要守住的六件事：

1. 先落用户消息，模型挂了这句话也不能丢
2. 失败返回 5xx，不是 200 + 假回复；失败行也要落库（刷新后看得见）
3. 上下文只放 status=ok 的 user/assistant 消息，失败行不能回灌给模型
4. 两句一次提交：回复 + 计数 + 用量；失败时「失败行 + 计数 + 失败用量」
5. 别人的 / 软删的会话 → 404，且一条消息都不落
6. 用量里的 model 以**响应**为准（服务端可能把 deepseek-chat 路由到 flash）
"""

from datetime import timedelta
from typing import Sequence

import pytest

from conftest import API, new_session

from app import app
from core.config import settings
from core.time import now_utc
from modules.lifeweb.ai.models import ChatMessage, ChatSession, UsageLog
from modules.lifeweb.ai.provider import FakeProvider, LLMError, LLMProvider, LLMReply
from modules.lifeweb.ai.router import get_provider


class _ErrorProvider(LLMProvider):
    """每次调用都失败的 provider —— 验证失败路径时不污染 FakeProvider 的语义。"""

    def __init__(self, error: LLMError) -> None:
        self.error = error
        self.calls: list[list[dict]] = []

    def chat(self, messages: Sequence[dict], **kwargs) -> LLMReply:
        self.calls.append(list(messages))
        raise self.error


@pytest.fixture()
def provider():
    """把模型依赖换成 FakeProvider；用完恢复，避免影响别的测试文件。"""
    fake = FakeProvider()
    app.dependency_overrides[get_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_provider, None)


def _use_failing(error: LLMError) -> _ErrorProvider:
    broken = _ErrorProvider(error)
    app.dependency_overrides[get_provider] = lambda: broken
    return broken


def _chat(client, message: str, session_id: int | None = None):
    body = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id
    return client.post(f"{API}/ai/chat", json=body)


def _rows(model, **filters):
    with new_session() as s:
        stmt = s.query(model)
        for key, value in filters.items():
            stmt = stmt.filter(getattr(model, key) == value)
        return stmt.all()


# ---------- 正常路径 ----------


def test_chat_without_session_creates_one_and_titles_from_message(client, provider):
    provider.queue(
        LLMReply(
            content="好的，牛奶记下了。",
            token_in=312,
            token_out=9,
            latency_ms=989,
            model="deepseek-chat",
        )
    )

    res = _chat(client, "明天下午三点去超市买牛奶")

    assert res.status_code == 200
    body = res.json()
    assert body["reply"] == "好的，牛奶记下了。"
    assert body["usage"] == {
        "token_in": 312,
        "token_out": 9,
        "latency_ms": 989,
        "model": "deepseek-chat",
        "cost_estimate": 0.0,
    }

    with new_session() as s:
        session = s.get(ChatSession, body["session_id"])
        assert session is not None
        assert session.user_id == 1
        # 首句话直接当标题（消息短于 30 字，原样保留）
        assert session.title == "明天下午三点去超市买牛奶"
        assert session.message_count == 2
        assert session.last_message_at is not None

    messages = _rows(ChatMessage, session_id=body["session_id"])
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "明天下午三点去超市买牛奶"
    assert messages[1].content == "好的，牛奶记下了。"
    assert messages[1].token_in == 312
    assert messages[1].token_out == 9


def test_chat_appends_to_existing_session(client, provider):
    created = client.post(f"{API}/ai/sessions", json={"title": "我的对话"}).json()
    provider.queue(LLMReply(content="第一句回复", token_in=10, token_out=2, latency_ms=10))
    provider.queue(LLMReply(content="第二句回复", token_in=20, token_out=3, latency_ms=11))

    first = _chat(client, "第一问", created["id"])
    second = _chat(client, "第二问", created["id"])

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"] == created["id"]

    with new_session() as s:
        session = s.get(ChatSession, created["id"])
        # 手动改过的标题不会被第一句话覆盖
        assert session.title == "我的对话"
        assert session.message_count == 4

    messages = _rows(ChatMessage, session_id=created["id"])
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]


def test_chat_passes_system_prompt_and_history(client, provider):
    provider.queue(LLMReply(content="第一句回复"))
    provider.queue(LLMReply(content="第二句回复"))

    first = _chat(client, "第一问")
    assert first.status_code == 200
    _chat(client, "第二问", first.json()["session_id"])

    assert len(provider.calls) == 2
    first = provider.calls[0]["messages"]
    second = provider.calls[1]["messages"]

    # 第一轮：system + 本次提问
    assert [m["role"] for m in first] == ["system", "user"]
    # 第二轮：system + 上一轮的一问一答 + 本次提问
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]

    system = second[0]["content"]
    assert "当前时间" in system
    assert "Asia/Shanghai" in system
    # 分类注入：来自 categories 表（conftest 种了 41 条）
    assert "待办" in system and "工作" in system
    assert "支出" in system and "餐饮" in system
    # A5 起提示词里出现了工具说明（正向强制「请你确认」），
    # 这里守住反面：只允许出现「不要声称已经保存」这种**禁止**句，
    # 不能出现「已经帮你保存好了」这种**宣称**句
    assert "不要声称已经保存" in system  # 反面规则还在
    for claim in ("已经帮你", "已为你", "已经记好"):
        assert claim not in system
    assert "调用工具帮用户创建待办" in system  # 工具规则本身必须发出去
    assert second[-1]["content"] == "第二问"


def test_chat_uses_reply_model_in_usage(client, provider):
    # 请求写的是 deepseek-chat，服务端实际路由到 flash —— 记账要记后者的名字
    provider.queue(LLMReply(content="好", model="deepseek-flash"))

    res = _chat(client, "你好")

    assert res.json()["usage"]["model"] == "deepseek-flash"
    logs = _rows(UsageLog)
    assert len(logs) == 1
    assert logs[0].model == "deepseek-flash"
    assert logs[0].purpose == "chat"


def test_cost_estimate_uses_configured_price(client, provider, monkeypatch):
    monkeypatch.setattr(settings, "llm_price_in_per_mtok", 2.0)
    monkeypatch.setattr(settings, "llm_price_out_per_mtok", 8.0)
    provider.queue(LLMReply(content="好", token_in=1000, token_out=500))

    res = _chat(client, "你好")

    # (1000 * 2 + 500 * 8) / 1_000_000 = 0.006
    assert res.json()["usage"]["cost_estimate"] == 0.006
    logs = _rows(UsageLog)
    assert float(logs[0].cost_estimate) == pytest.approx(0.006)


# ---------- 校验与会话归属 ----------


@pytest.mark.parametrize(
    "message",
    ["", "   ", "长" * 2001],
)
def test_chat_rejects_invalid_message(client, provider, message):
    res = _chat(client, message)

    assert res.status_code == 422
    assert res.json()["code"] == "VALIDATION_ERROR"
    # 参数没过，就一步都不该走：模型没被调用
    assert provider.calls == []


def test_chat_returns_404_for_unknown_session(client, provider):
    res = _chat(client, "你好", 9999)

    assert res.status_code == 404
    assert provider.calls == []


def test_chat_does_not_write_to_others_session(client, provider):
    with new_session() as s:
        other = ChatSession(user_id=2, title="别人的会话")
        s.add(other)
        s.commit()
        other_id = other.id

    res = _chat(client, "偷偷说话", other_id)

    assert res.status_code == 404
    assert provider.calls == []
    assert _rows(ChatMessage, session_id=other_id) == []


def test_chat_returns_404_for_soft_deleted_session(client, provider):
    with new_session() as s:
        deleted = ChatSession(user_id=1, title="已删", deleted_at=None)
        deleted.deleted_at = now_utc() - timedelta(hours=1)
        s.add(deleted)
        s.commit()
        deleted_id = deleted.id

    res = _chat(client, "你好", deleted_id)

    assert res.status_code == 404
    assert provider.calls == []
    assert _rows(ChatMessage, session_id=deleted_id) == []


# ---------- 失败路径 ----------


def test_timeout_returns_504_and_keeps_failed_rows(client):
    _use_failing(LLMError("TIMEOUT", "模型响应超时，请稍后重试", 504))

    res = _chat(client, "你好")

    assert res.status_code == 504
    assert res.json()["code"] == "TIMEOUT"

    with new_session() as s:
        session = s.query(ChatSession).one()
        assert session.message_count == 2
        assert session.last_message_at is not None

    messages = _rows(ChatMessage)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "你好"
    assert messages[1].status == "failed"
    assert messages[1].content is None
    assert messages[1].error_message == "模型响应超时，请稍后重试"

    logs = _rows(UsageLog)
    assert len(logs) == 1
    assert logs[0].status == "failed"
    assert logs[0].error_code == "TIMEOUT"


def test_rate_limit_returns_429(client):
    _use_failing(LLMError("RATE_LIMIT", "模型调用额度不足或请求过于频繁", 429))

    res = _chat(client, "你好")

    assert res.status_code == 429
    assert res.json()["code"] == "RATE_LIMIT"


def test_auth_error_returns_401_without_leaking_key(client):
    _use_failing(LLMError("AUTH", "模型服务认证失败，请检查 LLM_API_KEY", 401))

    res = _chat(client, "你好")

    assert res.status_code == 401
    assert "LLM_API_KEY" in res.json()["message"]
    # provider 的约定是 message 里不出现 key 本身
    assert settings.llm_api_key not in res.json()["message"]


def test_failed_message_is_not_sent_back_to_model(client):
    created = client.post(f"{API}/ai/sessions", json={}).json()
    _use_failing(LLMError("TIMEOUT", "模型响应超时，请稍后重试", 504))
    _chat(client, "第一问", created["id"])

    # 第二次换成正常 provider，检查上下文里没有上一条失败的助手消息
    fake = FakeProvider([LLMReply(content="这回成功了")])
    app.dependency_overrides[get_provider] = lambda: fake

    res = _chat(client, "第二问", created["id"])

    assert res.status_code == 200
    roles = [m["role"] for m in fake.calls[0]["messages"]]
    # system + 两个 user（失败的 assistant 被过滤掉）
    assert roles == ["system", "user", "user"]
    assert fake.calls[0]["messages"][-1]["content"] == "第二问"


def test_missing_api_key_returns_503(client, monkeypatch):
    # 不覆盖 get_provider，走真实的 build_provider
    app.dependency_overrides.pop(get_provider, None)
    monkeypatch.setattr(settings, "llm_api_key", "")

    res = _chat(client, "你好")

    assert res.status_code == 503
    assert res.json()["code"] == "NOT_CONFIGURED"
    # 配置问题不应留下半条消息
    assert _rows(ChatMessage) == []
