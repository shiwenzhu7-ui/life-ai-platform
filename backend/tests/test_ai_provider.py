"""A2：模型调用层（`modules/lifeweb/ai/provider.py`）。

这一层不值得「手动点一次看看」——它要保证的是**失败时的行为**：
401 不泄露 key、429 只重试一次、超时和坏 JSON 各自映射成可读的错误。
所以全部用 `httpx.MockTransport` 拦请求，不联网、不花钱、可重复。

真机连通（用 .env 里的 key 真调一次）不放在这里，单独跑，否则每次 pytest 都烧 token。
"""

import json

import httpx
import pytest

from core.config import settings
from modules.lifeweb.ai.provider import (
    DeepSeekProvider,
    FakeProvider,
    LLMError,
    LLMReply,
    build_provider,
)

KEY = "sk-test-not-a-real-key-0000"


def make_provider(handler, **kwargs) -> DeepSeekProvider:
    """造一个走 MockTransport 的 provider；retry_backoff=0 免得测试白等。"""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DeepSeekProvider(
        api_key=KEY,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        http_client=client,
        retry_backoff=0,
        **kwargs,
    )


def ok_body(content="好", *, tool_calls=None, model="deepseek-chat") -> dict:
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop" if tool_calls is None else "tool_calls",
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
    }


# ---------- FakeProvider ----------


def test_fake_provider_returns_queued_replies_in_order():
    fake = FakeProvider([LLMReply(content="第一句"), LLMReply(content="第二句")])

    assert fake.chat([{"role": "user", "content": "hi"}]).content == "第一句"
    assert fake.chat([{"role": "user", "content": "hi"}]).content == "第二句"


def test_fake_provider_records_call_arguments():
    fake = FakeProvider([LLMReply(content="好")])
    tools = [{"type": "function", "function": {"name": "query_tasks"}}]

    fake.chat(
        [{"role": "user", "content": "我有哪些待办"}],
        tools=tools,
        temperature=0.2,
        max_tokens=128,
    )

    call = fake.calls[0]
    assert call["messages"][0]["content"] == "我有哪些待办"
    assert call["tools"] == tools
    assert call["temperature"] == 0.2
    assert call["max_tokens"] == 128


def test_fake_provider_raises_when_exhausted():
    fake = FakeProvider()

    with pytest.raises(LLMError) as err:
        fake.chat([{"role": "user", "content": "hi"}])

    assert err.value.code == "FAKE_EXHAUSTED"


# ---------- 请求形状 ----------


def test_request_shape_and_auth_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    provider = make_provider(handler)
    provider.chat(
        [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "你好"}],
        temperature=0.3,
        max_tokens=256,
    )

    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    body = seen["body"]
    assert body["model"] == "deepseek-chat"
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 256
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "tools" not in body  # 没传就不该出现，否则部分厂商会报错


def test_tools_are_forwarded_when_given():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    tools = [{"type": "function", "function": {"name": "query_tasks", "parameters": {}}}]
    make_provider(handler).chat([{"role": "user", "content": "hi"}], tools=tools)

    assert seen["body"]["tools"] == tools


# ---------- 响应解析 ----------


def test_parse_content_and_usage():
    def handler(request):
        return httpx.Response(200, json=ok_body(content="晚上好", model="deepseek-chat"))

    reply = make_provider(handler).chat([{"role": "user", "content": "hi"}])

    assert reply.content == "晚上好"
    assert reply.tool_calls == []
    assert (reply.token_in, reply.token_out) == (9, 1)
    assert reply.model == "deepseek-chat"
    assert reply.latency_ms >= 0
    assert reply.raw["id"] == "chatcmpl-test"


def test_parse_tool_calls_with_none_content():
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "query_tasks", "arguments": '{"status":"todo"}'},
        }
    ]

    def handler(request):
        return httpx.Response(200, json=ok_body(content=None, tool_calls=calls))

    reply = make_provider(handler).chat([{"role": "user", "content": "我有哪些待办"}])

    assert reply.content is None
    assert reply.tool_calls == [
        {"id": "call_1", "name": "query_tasks", "arguments": '{"status":"todo"}'}
    ]


# ---------- 失败路径 ----------


def test_401_maps_to_auth_error_without_leaking_key():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Authentication Fails"}})

    with pytest.raises(LLMError) as err:
        make_provider(handler).chat([{"role": "user", "content": "hi"}])

    assert err.value.code == "AUTH"
    assert err.value.status_code == 401
    assert KEY not in err.value.message
    assert "LLM_API_KEY" in err.value.message


def test_429_is_retried_once_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json=ok_body(content="重试成功"))

    reply = make_provider(handler).chat([{"role": "user", "content": "hi"}])

    assert reply.content == "重试成功"
    assert calls["n"] == 2


def test_500_twice_stops_after_two_attempts():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"message": "boom"}})

    with pytest.raises(LLMError) as err:
        make_provider(handler).chat([{"role": "user", "content": "hi"}])

    assert err.value.code == "UPSTREAM"
    assert calls["n"] == 2  # 只重试一次，不无限重试


def test_timeout_maps_to_timeout_error():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMError) as err:
        make_provider(handler).chat([{"role": "user", "content": "hi"}])

    assert err.value.code == "TIMEOUT"
    assert err.value.status_code == 504


def test_connection_error_maps_to_upstream_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError) as err:
        make_provider(handler).chat([{"role": "user", "content": "hi"}])

    assert err.value.code == "UPSTREAM"


def test_non_json_body_maps_to_bad_response():
    def handler(request):
        return httpx.Response(200, content=b"<html>gateway</html>")

    with pytest.raises(LLMError) as err:
        make_provider(handler).chat([{"role": "user", "content": "hi"}])

    assert err.value.code == "BAD_RESPONSE"


def test_empty_choices_maps_to_bad_response():
    def handler(request):
        return httpx.Response(200, json={"model": "deepseek-chat", "choices": []})

    with pytest.raises(LLMError) as err:
        make_provider(handler).chat([{"role": "user", "content": "hi"}])

    assert err.value.code == "BAD_RESPONSE"


# ---------- 工厂 ----------


def test_build_provider_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")

    with pytest.raises(LLMError) as err:
        build_provider()

    assert err.value.code == "NOT_CONFIGURED"
    assert err.value.status_code == 503


def test_build_provider_uses_configured_values():
    provider = build_provider()

    assert isinstance(provider, DeepSeekProvider)
    assert provider.model == settings.llm_model
    assert provider.base_url == settings.llm_base_url.rstrip("/")
    provider.close()
