"""A5 第 4 步：把模型的「工具申请」变成一张待确认的草稿。

这是 A5 里**唯一新增的一层编排**，把前几步的零件接起来：

    provider.chat(messages, tools=ALL_TOOLS)   ← 第 1 步的说明书，第 4 步才真正传出去
        ↓  reply.tool_calls
    parse_tool_calls(reply)                    ← 第 2 步的审单员
        ↓  ToolDraft | 坏参数（带错误原因）
    create_draft(db, draft)                    ← 第 3 步的存档
        ↓  PendingAction（已 flush，**未 commit**）

为什么单独一个文件、不直接塞进 service.chat：
service 的职责是「一次问答的事务边界」，这段是「一次问答里工具这一支的花活」，
混在一起之后 service.chat 会长到没法读。这里只依赖 provider/pending/action_schema，
**不知道 HTTP，也不知道 FastAPI**，所以能脱离接口单独测。

五条必须写下来的约束：

1. **正常路径只调一次模型。** `tools=` 一直带着（见 tools_available）；
   第一次没给 tool_calls 就直接结束 —— 99% 的对话是聊天，不该为它们多花一次钱。

2. **只有「交了工具调用、但参数不合格」才重试，最多一次。**
   网络超时重试由 provider 内部负责（429/5xx 重试 1 次），那是「这次没问到」；
   参数不合格是「问到了但答得不对」，后果是用户看到一句空话、卡片没了。
   两种错误重试的理由不同，不能混在一起处理。重试仍走 provider 的 `messages=`
   接口把错误原话喂回去，不新增 provider 方法。

3. **模型先要了工具、又交了不合格的参数，不当成「它改主意了」，当成失败。**
   第二轮模型完全可能只回一段文字、一个 tool_call 都不给（放弃重试）。
   如果这时把文字当成功回复，用户看到的是「好的，已经帮你记下了」+ 没有卡片，
   库里也什么都没有 —— 这是 A5 最危险的状态。所以重试轮只要没有可用草稿，
   就按 `MALFORMED_TOOL_ARGS` 失败处理（`ok=False`）。

4. **模型完全没想调工具（`has_calls=False`）是成功。**
   `ok=True`、`draft=None`，调用方把 content 当普通回复显示。这是最常见的路径，
   不能因为「没有草稿」就报错。

5. **参数错误的原文要留在 `ToolFlow.rejection` 里。**
   调用方（service）会把它写进助手消息的 `error_message` 列，
   这样以后查「为什么模型又没给时区」不用翻日志。

`MAX_DRAFTS_PER_REPLY == 1`：一次只接一张卡。模型嘴上说「我不调工具」
却仍然带了 tool_calls，那就按调用走（`has_calls` 优先）—— 以行为为准，不以措辞为准。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy.orm import Session

from modules.lifeweb.ai.action_schema import (
    PAYLOAD_MODELS,
    ToolDraft,
    parse_tool_calls,
)
from modules.lifeweb.ai.models import ChatSession, PendingAction
from modules.lifeweb.ai.pending import create_draft
from modules.lifeweb.ai.provider import LLMError, LLMProvider, LLMReply
from modules.lifeweb.ai.tools import ALL_TOOLS, TOOL_NAMES

# 重试时塞给模型的错误原文上限：完整校验信息可能几百字，太长反而盖住重点
_REJECTION_MAX = 300

# 参数错误原文里会带上一整段 JSON（pydantic 的 input_value），用户不需要看
_ARGS_DUMP = re.compile(r"\s*input_value=.*?\s*input_type=[^,)]*", re.S)


@dataclass
class ToolFlow:
    """一次问答里「工具这一支」的结果。

    - `ok=True, draft is None`      → 正常聊天，content 直接显示
    - `ok=True, draft is not None`  → 有卡片待确认（草稿已 flush，未提交）
    - `ok=False`                    → 模型想调工具但参数不行，调用方按失败处理
    """

    reply: LLMReply
    draft: ToolDraft | None = None
    pending: PendingAction | None = None
    has_calls: bool = False
    rejection: str | None = None
    ok: bool = True
    retried: bool = False
    last_error: LLMError | None = None
    """第一次调用的失败原因（重试被短路时带上，调用方据此抛 5xx）。"""

    @property
    def error_message(self) -> str | None:
        """给助手消息的 error_message 列用。"""
        return self.rejection[:_REJECTION_MAX] if self.rejection else None


def tools_available(provider: LLMProvider) -> bool:
    """这个 provider 的 chat 收不收 `tools` 参数。

    用**能力判断**而不是类型判断（isinstance），这样测试里换成别的假 provider 时，
    只要它的 `chat` 不收 `tools`，就自动退回纯聊天，不必改业务代码。
    DeepSeekProvider / FakeProvider 都声明了 `tools=None`，返回 True。
    """
    code = getattr(getattr(provider.chat, "__func__", None), "__code__", None)
    if code is None:
        # 内置函数 / mock 对象拿不到 __code__：按「支持」处理，
        # 真传进去不认会直接 TypeError，比悄悄降级更容易发现
        return True
    return "tools" in code.co_varnames


def run_tool_flow(
    db: Session,
    provider: LLMProvider,
    messages: list[dict],
    *,
    user_id: int,
    session: ChatSession,
    message_id: int,
) -> ToolFlow:
    """发请求（必要时重试一次）→ 解析草稿 → 存成 pending 行。

    `message_id` 是**本次助手回复**的消息 id，草稿挂在它下面，用来回答
    「这张卡是哪句话产生的」。注意它必须是已经 flush 过的行（有 id）。
    """
    tools = ALL_TOOLS if tools_available(provider) else None

    try:
        reply = provider.chat(messages, tools=tools)
    except LLMError as exc:
        return ToolFlow(
            reply=_empty_reply(),
            last_error=exc,
            ok=False,
            rejection=exc.message,
        )

    # 没想调工具：99% 的对话走这里，不再多发一次请求
    if not (getattr(reply, "tool_calls", None) or []):
        return ToolFlow(reply=reply, has_calls=False)

    drafts = parse_tool_calls(reply)
    if not drafts:
        # 交了工具调用但参数不合格 → 把错误原文喂回去，让它原地改一次。
        # 前提是「错误出在参数上」：工具名不存在 / arguments 根本不是字符串，
        # 说明模型这一支已经跑偏，同样的说明书再发一次也改不好，直接失败更省一次钱。
        rejection = _rejection_of(reply) or "工具参数没有通过校验"
        if _is_hopeless(reply):
            return ToolFlow(
                reply=reply,
                has_calls=True,
                rejection=rejection,
                ok=False,
            )

        try:
            reply = provider.chat(_retry_messages(messages, rejection), tools=tools)
        except LLMError as exc:
            return ToolFlow(
                reply=_empty_reply(),
                has_calls=True,
                rejection=f"{rejection}；重试时模型调用失败：{exc.message}",
                ok=False,
                retried=True,
                last_error=exc,
            )

        drafts = parse_tool_calls(reply)
        if not drafts:
            return ToolFlow(
                reply=reply,
                has_calls=True,
                rejection=rejection,
                ok=False,
                retried=True,
            )
        retried = True
    else:
        retried = False

    pending = create_draft(
        db,
        user_id=user_id,
        session=session,
        message_id=message_id,
        draft=drafts[0],
    )
    return ToolFlow(
        reply=reply,
        draft=drafts[0],
        pending=pending,
        has_calls=True,
        retried=retried,
    )


def _is_hopeless(reply: LLMReply) -> bool:
    """这次失败是不是「重试也没用」的那类：工具名不认识 / arguments 不是 JSON 字符串。

    参数校验失败（缺 title、时间没时区）才值得重试 —— 那是模型「知道要调什么、
    只是格式不对」；工具名对不上说明它根本没理解这份说明书。
    """
    for call in getattr(reply, "tool_calls", None) or []:
        if not isinstance(call, dict):
            return True
        if call.get("name") not in TOOL_NAMES:
            return True
        if not isinstance(call.get("arguments"), str):
            return True
    return False


def _empty_reply() -> LLMReply:
    """模型根本没回话时的占位（调用方拿它的 content 当正文，所以给句人话）。"""
    return LLMReply(content="模型没有返回内容，请稍后重试。")


def _rejection_of(reply: LLMReply) -> str | None:
    """从被丢弃的 tool_call 里挖出「为什么不行」。

    解析器本身的约定是「只丢弃、不抛异常」，所以原因只可能在两处：
    arguments 不是合法 JSON（json 库里带 message），或者参数校验失败
    （pydantic 的 ValidationError 带 first line + 字段定位）。
    挖不出来就返回一句通用原因 —— 重试仍然值得做。
    """
    for call in getattr(reply, "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if name not in TOOL_NAMES:
            # 幻觉工具名不是「参数」问题，重试也没用
            return f"不支持的工具：{name}"
        arguments = call.get("arguments")
        if not isinstance(arguments, str):
            return "工具参数不是字符串"
        data = _loads(arguments)
        if data is _INVALID_JSON:
            return "工具参数不是合法 JSON"
        if not isinstance(data, dict):
            return "工具参数不是对象"
        detail = _validate_message(name, data)
        if detail:
            return detail
    return None


_INVALID_JSON = object()


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _INVALID_JSON


def _validate_message(tool_name: str, data: dict) -> str | None:
    """跑一次校验，把 pydantic 的报错压成一行人话。"""
    model = PAYLOAD_MODELS.get(tool_name)
    if model is None:
        return f"不支持的工具：{tool_name}"
    try:
        model(**data)
    except Exception as exc:  # noqa: BLE001 - 任何校验异常都只要第一行
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        cleaned = _ARGS_DUMP.sub("", first).strip()
        return cleaned or "参数校验失败"
    return None


def _retry_messages(messages: Sequence[dict], rejection: str | None) -> list[dict]:
    """重试用的 messages：系统提示 + 原问题 + 一条错误说明。

    不带历史：历史里那个坏参数已经被解析器丢掉，重发一遍反而让模型以为自己是对的。
    """
    reason = (rejection or "参数格式不对")[:_REJECTION_MAX]
    head = list(messages[:1])
    last = messages[-1]["content"] if messages else ""
    return [
        *head,
        {
            "role": "user",
            "content": f"{last}\n\n（系统提示：上一次的工具参数没有通过校验：{reason}。"
            "请按工具要求的格式重新给出一次；如果确实无法确定，就直接用文字反问用户。）",
        },
    ]


__all__ = ["ToolFlow", "run_tool_flow", "tools_available"]