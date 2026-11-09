"""A4：一句话 → 模型回话 → 落库；A5 第 4 步：顺带把工具申请存成待确认草稿。

这个文件是「编排层」：crud 只管读写，provider 只管发请求，
**顺序和事务边界在这里决定**。四条规则值得单独写下来：

1. **模型调用不能进数据库事务。**
   如果 `with db.begin(): provider.chat(...)`，那 1~60 秒的网络等待里
   数据库锁一直不释放，同一会话的另一次请求（或另一个标签页）会卡住。
   所以用户消息先单独 commit，模型调用在事务外，回复 + 计数 + 用量再一起 commit。

2. **先存用户的话，再调模型。**
   反过来写的话，模型超时那一刻用户刚打的字就没了。宁可留下一条
   「有提问、没回复」的记录（失败消息也会落一行），也不能把输入弄丢。

3. **失败也留痕。**
   助手消息写 status=failed + error_message，用量写 status=failed + error_code。
   前端刷新时能看到「这条失败了」，而不是消息凭空消失。

4. **草稿和三件事在同一个 commit 里落地**（A5）。
   助手回复、会话计数、用量、ai_pending_actions 一起提交：
   不能出现「回复里说有一条待办、库里却没有草稿」，也不能反过来。

上下文窗口：最近 `HISTORY_LIMIT` 条消息（≈10 轮），不做摘要。
`docs/AI设计.md:42` 写的是「超长保留 6 轮 + 摘要」——摘要要额外一次模型调用，
等真有长对话再补，现在先简单。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from core.errors import AppError, not_found
from core.time import now_beijing, now_utc
from modules.lifeweb.ai import crud
from modules.lifeweb.ai.models import ChatMessage, ChatSession, PendingAction
from modules.lifeweb.ai.prompts import build_system_prompt
from modules.lifeweb.ai.provider import LLMError, LLMProvider, LLMReply
from modules.lifeweb.ai.schema import DEFAULT_TITLE
from modules.lifeweb.ai.tool_flow import run_tool_flow

# 最近多少条消息进上下文（一轮 = 用户 + 助手，所以 20 条 ≈ 10 轮）
HISTORY_LIMIT = 20

# 首条消息自动生成标题：取前 30 字，超长加省略号（列上限 120，够用）
_TITLE_MAX = 30

# 失败消息的 error_message 列是 String(500)，超了会在 MySQL 严格模式下报错
_ERROR_MAX = 500

# 助手消息的回退正文：模型只调工具、一个字都没说时，气泡里不能是空白
# （前端 v-for 渲染 null 会报错；而且用户需要知道「卡片是怎么来的」）
FALLBACK_REPLY = "我整理了一条待办，请你确认。"

# 改动类草稿的兜底正文。不能复用上面那句：「我整理了一条待办」在
# 改待办时是**假话** —— 用户会以为多出来了一条，而不是某一条被改了。
FALLBACK_REPLY_UPDATE = "我整理了一条修改，请你确认。"

# 工具参数始终不合格时返回的正文与错误码
MALFORMED_REASON = "模型给出的工具参数不合格，这一轮没有生成待确认卡片，请再试一次。"

# 模型两次都交了坏参数、且一个字都没说时，落库的正文（用户刷新后看得见）
FALLBACK_FAILED = "（这次没能生成待确认卡片）"


@dataclass
class ChatTurn:
    """一次问答的结果 —— router 从这里拼响应，不直接摸 ORM 细节。"""

    session: ChatSession
    assistant_message: ChatMessage
    token_in: int
    token_out: int
    latency_ms: int
    model: str
    cost_estimate: float
    pending: PendingAction | None = None


def chat(
    db: Session,
    user_id: int,
    *,
    session_id: int | None,
    message: str,
    provider: LLMProvider,
) -> ChatTurn:
    """发一句话，拿回复。失败抛 AppError（code 与 LLMError 一致）。"""
    session = _resolve_session(db, user_id, session_id)

    # ---------- 1. 先落用户消息 ----------
    user_message = crud.add_message(
        db, user_id=user_id, session_id=session.id, role="user", content=message
    )
    _maybe_rewrite_title(session, message)
    db.commit()
    db.refresh(user_message)

    # ---------- 2. 拼上下文（含刚存进去的这句话） ----------
    history = crud.recent_messages(db, user_id, session.id, limit=HISTORY_LIMIT)
    categories = crud.system_categories(db)
    llm_messages = _build_llm_messages(history, categories=categories)

    # 助手这一行先落库拿到 id：草稿要挂在它下面（「这张卡是哪句话产生的」），
    # 而正文、token、状态都得等模型回来才知道。**注意这里只 flush，不 commit。**
    # status 只能是 ok/failed（枚举），先写 ok —— 真失败时第 4 段再改成 failed。
    # 风险：commit 之前进程崩掉的话，会话里会留下一行 content 为空的 ok 消息，
    # 下一轮拼上下文时会因为「正文为空」被跳过，不会影响模型。
    assistant = crud.add_message(
        db,
        user_id=user_id,
        session_id=session.id,
        role="assistant",
        content=None,
        status="ok",
    )

    # ---------- 3. 调模型（事务外） ----------
    try:
        flow = run_tool_flow(
            db,
            provider,
            llm_messages,
            user_id=user_id,
            session=session,
            message_id=assistant.id,
        )
    except LLMError as exc:
        db.rollback()  # 丢掉那条 content 为空的助手行和可能的草稿
        _record_failure(db, session, user_id, exc)
        raise AppError(exc.code, exc.message, exc.status_code) from exc

    if flow.last_error is not None:
        # run_tool_flow 自己吞掉了 provider 的网络/状态码错误（好让调用方统一收口），
        # 这里跟上面那条路径做同样的事：回滚半成品行、落一条 failed 消息、抛 AppError
        db.rollback()
        _record_failure(db, session, user_id, flow.last_error)
        raise AppError(flow.last_error.code, flow.last_error.message, flow.last_error.status_code)

    # ---------- 4. 回复 + 草稿 + 计数 + 用量，一次提交 ----------
    # 失败（flow.ok=False：模型两次都交了不合格的工具参数）时这条消息仍是用户
    # 这一轮唯一的记录，所以照样落库、标 failed，并把模型的原文和失败原因一起存下 ——
    # 用户刷新后能看到「它说了一句什么、为什么没成」，而不是一条凭空消失的提问。
    _apply_reply(assistant, flow.reply, failed=not flow.ok)
    if not flow.ok:
        assistant.content = flow.reply.content or FALLBACK_FAILED
        assistant.error_message = flow.error_message
    crud.touch_session(db, session, added=2, at=now_utc())

    cost = _estimate_cost(flow.reply.token_in, flow.reply.token_out)
    model = flow.reply.model or session.model or settings.llm_model
    crud.add_usage_log(
        db,
        user_id=user_id,
        model=model,
        token_in=flow.reply.token_in,
        token_out=flow.reply.token_out,
        cost_estimate=cost,
        latency_ms=flow.reply.latency_ms,
        status="failed" if not flow.ok else "ok",
        error_code=None if flow.ok else "MALFORMED_TOOL_ARGS",
    )
    db.commit()
    db.refresh(assistant)
    if flow.pending is not None:
        db.refresh(flow.pending)

    return ChatTurn(
        session=session,
        assistant_message=assistant,
        token_in=flow.reply.token_in,
        token_out=flow.reply.token_out,
        latency_ms=flow.reply.latency_ms,
        model=model,
        cost_estimate=cost,
        pending=flow.pending,
    )


# ---------- 内部 ----------


def _apply_reply(msg: ChatMessage, reply: LLMReply, *, failed: bool) -> None:
    """把模型的东西写到助手消息上（正文为空时的处理在这里收口）。

    - 有正文：原样用
    - 没正文但有草稿：用 FALLBACK_REPLY，让气泡里能看见一句话
    - 没正文也没草稿（模型回了空串）：`(无回复)`，前端据此不渲染空气泡
    """
    content = (reply.content or "").strip()
    if content:
        msg.content = content
    elif reply.tool_calls:
        msg.content = _fallback_for(reply.tool_calls)
    else:
        msg.content = "(无回复)"
    msg.tool_calls = reply.tool_calls or None
    msg.token_in = reply.token_in
    msg.token_out = reply.token_out
    msg.latency_ms = reply.latency_ms
    msg.status = "failed" if failed else "ok"


def _fallback_for(tool_calls: list[dict[str, Any]]) -> str:
    """按工具名挑一句兜底正文。

    模型常常交了 tool_calls 却一个字不说。此时气泡总得有句话，
    但话不能瞎说：产出「新建待办」草稿时和「改一条待办」时说的一句话不一样。

    tool_call 的形状有两种：DeepSeek 原始协议是扁平`{"id","name","arguments"}`），
    OpenAI 风格是嵌套的（`{"function": {"name": ...}}`）。两种都认，
    否则线上和假 provider 会用上不一样的那句话。
    """
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name") or call.get("name")
        if name == "update_task":
            return FALLBACK_REPLY_UPDATE
    return FALLBACK_REPLY


def _resolve_session(
    db: Session, user_id: int, session_id: int | None
) -> ChatSession:
    """有 id 就取（别人的 / 软删的 → 404），没有就新建。"""
    if session_id is None:
        return crud.create_session(
            db, user_id, title=DEFAULT_TITLE, model=settings.llm_model
        )
    session = crud.get_session(db, user_id, session_id)
    if session is None:
        raise not_found("会话不存在")
    return session


def _maybe_rewrite_title(session: ChatSession, message: str) -> None:
    """空会话的第一句话顺便当标题。

    `models/chat_session.py:5` 约定的「首条用户消息落库后截前 30 字」就在这里做。
    只改「还没说过话 + 仍是默认标题」的会话：
    用户手动改过标题的（title != 默认）不能被覆盖。
    """
    if session.last_message_at is not None or session.title != DEFAULT_TITLE:
        return
    text = " ".join(message.split())
    session.title = text[:_TITLE_MAX] + ("…" if len(text) > _TITLE_MAX else "")


def _build_llm_messages(
    history: list[ChatMessage],
    *,
    categories: dict[str, list[str]] | None = None,
) -> list[dict]:
    """拼成 OpenAI 兼容的 messages 数组。

    - 第一条是 system（时间 + 分类 + 工具规则 + 说话规则）
    - 只放 user / assistant 且 status=ok 且有正文的消息：
      失败行、status=pending 的半成品行、以及 content 为 `(无回复)` 的占位都不能放进去
      —— 模型看到自己上次「什么都没说」会开始瞎猜。
    - assistant 行如果带 tool_calls，就**必须**跟着一条 role=tool 的回复，
      否则 DeepSeek 会直接拒请求。A5 目前不把 tool_calls 回灌（卡片本身就是对话的一部分，
      用户点确认后第 5 步会让前端追加一句正常的用户消息），所以这里同样跳过。
    """
    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt(
                now_beijing(), categories=categories, tools_enabled=True
            ),
        }
    ]
    for row in history:
        if row.status != "ok" or row.role not in ("user", "assistant"):
            continue
        if row.tool_calls:
            continue
        text = (row.content or "").strip()
        if not text or text == _NO_REPLY:
            continue
        messages.append({"role": row.role, "content": text})
    return messages


_NO_REPLY = "(无回复)"


def _record_failure(
    db: Session, session: ChatSession, user_id: int, exc: LLMError
) -> None:
    """失败也留两行：助手消息（failed）+ 用量（failed）。"""
    crud.add_message(
        db,
        user_id=user_id,
        session_id=session.id,
        role="assistant",
        content=None,
        status="failed",
        error_message=exc.message[:_ERROR_MAX],
    )
    crud.touch_session(db, session, added=2, at=now_utc())
    crud.add_usage_log(
        db,
        user_id=user_id,
        model=session.model or settings.llm_model,
        status="failed",
        error_code=exc.code,
    )
    db.commit()


def _estimate_cost(token_in: int, token_out: int) -> float:
    """按配置单价估费用；单价为 0（默认）时返回 0。

    单位是「元 / 百万 token」，`Numeric(10, 6)` 只保留 6 位小数，
    所以最后 round(…, 6)：不 round 会带一长串浮点尾巴，存进去被静默截断。
    """
    total = (
        token_in * settings.llm_price_in_per_mtok
        + token_out * settings.llm_price_out_per_mtok
    ) / 1_000_000
    return round(total, 6)


__all__ = ["chat", "ChatTurn", "HISTORY_LIMIT"]
