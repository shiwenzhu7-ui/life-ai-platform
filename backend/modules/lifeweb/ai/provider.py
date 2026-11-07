"""V1.1 A2：把「调模型」收敛到一个接口后面。

业务代码（A3 会话接口、A4 工具、A5 确认流、A6 真接模型）一律只写
`provider.chat(messages, ...)`，不直接碰 httpx，也不自己拼 URL —— 否则会出现三个麻烦：

1. **测试要联网要花钱**：86 条回归里只要有一条真调模型，跑一次就烧 token；
2. **换模型要改多处**：DeepSeek → 别家（或以后本地模型），得满项目找拼请求的地方；
3. **失败没法统一处理**：401 / 429 / 超时 / 模型吐了段坏 JSON，各写一遍。

本文件提供五样东西：

    LLMReply        一次调用的结果（内容 + 工具调用 + token + 耗时）
    LLMError        统一的失败类型，带 code，**绝不含 key**
    LLMProvider     抽象接口，只有一个方法 chat()
    DeepSeekProvider 真调（OpenAI 兼容的 /chat/completions）
    FakeProvider    假调（预置回复排队，测试用，不联网）
    build_provider() 工厂：有 key 给真实现，没 key 直接报错

**明确不做**：流式（V1.2 再说）、embedding（V1.3 的事）、写 `ai_usage_logs`
（计量属于业务层 A6，provider 保持纯函数式才好测）。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# 连模型：连接 5 秒够用，读要 60 秒 —— 模型吐一段长回复时几秒到几十秒都正常
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=5.0)

# 这些状态码值得重试一次：限流 + 上游自己抖动
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class LLMReply:
    """一次模型调用的结果。

    `content` 与 `tool_calls` 可能只有其一：模型可以只说话，也可以只调工具，
    但不能两样都没有（那种情况按坏响应处理）。
    """

    content: str | None
    tool_calls: list[dict] = field(default_factory=list)
    token_in: int = 0
    token_out: int = 0
    latency_ms: int = 0
    model: str = ""
    # 原始响应，排障时用；**不含 key**，key 只在请求头里
    raw: dict = field(default_factory=dict)


class LLMError(Exception):
    """模型调用失败。

    code 取值：AUTH / NOT_CONFIGURED / RATE_LIMIT / TIMEOUT / UPSTREAM / BAD_RESPONSE。
    约定：message 一定能安全回给前端 —— 不出现 key、不出现完整请求体（NFR 11.1）。
    """

    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class LLMProvider(ABC):
    """模型提供方的唯一接口。业务层只依赖它，不依赖具体实现。"""

    @abstractmethod
    def chat(
        self,
        messages: Sequence[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 800,
    ) -> LLMReply:
        """发一轮对话，返回模型的回复。失败抛 LLMError。"""


class DeepSeekProvider(LLMProvider):
    """DeepSeek 实现（OpenAI 兼容协议，换 base_url 就是换厂商）。

    :param http_client: 传进来就用它（测试用 MockTransport 拦请求），不传就自己建。
    :param retry_backoff: 重试前等几秒；测试传 0，免得白等。
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        http_client: httpx.Client | None = None,
        retry_backoff: float = 0.5,
        max_attempts: int = 2,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise LLMError("NOT_CONFIGURED", "AI 助手未配置 LLM_API_KEY", 503)
        self.api_key = key
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self._client = http_client
        self._retry_backoff = retry_backoff
        self._max_attempts = max_attempts

    # ---------- 对外 ----------

    def chat(
        self,
        messages: Sequence[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 800,
    ) -> LLMReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        started = time.perf_counter()
        response = self._send(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._raise_for_status(response)
        data = self._decode(response)
        return self._to_reply(data, latency_ms)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # ---------- 内部 ----------

    @property
    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=DEFAULT_TIMEOUT)
        return self._client

    def _send(self, payload: dict) -> httpx.Response:
        """发请求；429 / 5xx 重试一次，其余状态原样返回交给 _raise_for_status。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._http().post(self._url, json=payload, headers=headers)
            except httpx.TimeoutException:
                raise LLMError("TIMEOUT", "模型响应超时，请稍后重试", 504) from None
            except httpx.HTTPError:
                raise LLMError("UPSTREAM", "无法连接模型服务，请检查网络", 502) from None

            if response.status_code in RETRYABLE_STATUS and attempt < self._max_attempts:
                logger.warning(
                    "模型服务返回 %s，%.1fs 后重试（第 %s 次）",
                    response.status_code,
                    self._retry_backoff,
                    attempt,
                )
                time.sleep(self._retry_backoff)
                continue
            return response

        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        if status == 401:
            # 不能把 Authorization 头带进 message —— 前端能看到这句话
            raise LLMError("AUTH", "模型服务认证失败，请检查 LLM_API_KEY", 401)
        if status in (402, 429):
            raise LLMError("RATE_LIMIT", "模型调用额度不足或请求过于频繁", 429)
        raise LLMError("UPSTREAM", f"模型服务返回 HTTP {status}", 502)

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            raise LLMError("BAD_RESPONSE", "模型返回的不是合法 JSON", 502) from None
        if not isinstance(data, dict):
            raise LLMError("BAD_RESPONSE", "模型返回的 JSON 不是对象", 502)
        return data

    def _to_reply(self, data: dict, latency_ms: int) -> LLMReply:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("BAD_RESPONSE", "模型响应里没有 choices", 502)

        message = choices[0].get("message") or {}
        content = message.get("content")
        tool_calls = [
            {
                "id": call.get("id"),
                "name": (call.get("function") or {}).get("name"),
                "arguments": (call.get("function") or {}).get("arguments"),
            }
            for call in message.get("tool_calls") or []
        ]
        if not content and not tool_calls:
            raise LLMError("BAD_RESPONSE", "模型既没有回复内容也没有工具调用", 502)

        usage = data.get("usage") or {}
        return LLMReply(
            content=content,
            tool_calls=tool_calls,
            token_in=int(usage.get("prompt_tokens") or 0),
            token_out=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
            model=str(data.get("model") or self.model),
            raw=data,
        )


class FakeProvider(LLMProvider):
    """测试用假 provider：预置回复排队，按顺序取，不联网不花钱。

    用法：`FakeProvider([LLMReply(content="好")])`，或用 `queue()` 继续追加。
    `calls` 记录每次入参，方便断言「业务层到底发了什么给模型」。
    """

    def __init__(self, replies: Sequence[LLMReply] | None = None) -> None:
        self._replies: list[LLMReply] = list(replies or [])
        self.calls: list[dict] = []

    def queue(self, reply: LLMReply) -> "FakeProvider":
        self._replies.append(reply)
        return self

    def chat(
        self,
        messages: Sequence[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 800,
    ) -> LLMReply:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if not self._replies:
            raise LLMError(
                "FAKE_EXHAUSTED",
                "FakeProvider 没有更多预置回复（测试里请先 queue()）",
                500,
            )
        return self._replies.pop(0)


def build_provider() -> LLMProvider:
    """按配置造 provider。

    没配 key 时**抛错而不是静默降级**：AI 接口应当明确告诉用户「没配置」，
    而不是给一个永远回「好的」的假助手让人以为功能正常。
    """
    if not settings.llm_ready:
        raise LLMError("NOT_CONFIGURED", "AI 助手未配置 LLM_API_KEY", 503)
    return DeepSeekProvider(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )


__all__ = [
    "LLMProvider",
    "LLMReply",
    "LLMError",
    "DeepSeekProvider",
    "FakeProvider",
    "build_provider",
]
