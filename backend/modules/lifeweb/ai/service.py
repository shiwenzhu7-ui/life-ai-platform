"""A4：一句话 → 模型回话 → 落库。

这个文件是「编排层」：crud 只管读写，provider 只管发请求，
**顺序和事务边界在这里决定**。三条规则值得单独写下来：

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

上下文窗口：最近 `HISTORY_LIMIT` 条消息（≈10 轮），不做摘要。
`docs/AI设计.md:42` 写的是「超长保留 6 轮 + 摘要」——摘要要额外一次模型调用，
等真有长对话再补，现在先简单。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from core.config import settings
from core.errors import AppError, not_found
from core.time import now_beijing, now_utc
from modules.lifeweb.ai import crud
from modules.lifeweb.ai.chat_message import ChatMessage
from modules.lifeweb.ai.chat_session import ChatSession
from modules.lifeweb.ai.prompts import build_system_prompt
from modules.lifeweb.ai.provider import LLMError, LLMProvider
from modules.lifeweb.ai.schema import DEFAULT_TITLE

# 最近多少条消息进上下文（一轮 = 用户 + 助手，所以 20 条 ≈ 10 轮）
HISTORY_LIMIT = 20

# 首条消息自动生成标题：取前 30 字，超长加省略号（列上限 120，够用）
_TITLE_MAX = 30

# 失败消息的 error_message 列是 String(500)，超了会在 MySQL 严格模式下报错
_ERROR_MAX = 500


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
    llm_messages = _build_llm_messages(
        history, categories=crud.system_categories(db)
    )

    # ---------- 3. 调模型（事务外） ----------
    try:
        reply = provider.chat(llm_messages)
    except LLMError as exc:
        _record_failure(db, session, user_id, exc)
        raise AppError(exc.code, exc.message, exc.status_code) from exc

    # ---------- 4. 回复 + 计数 + 用量，一次提交 ----------
    now = now_utc()
    assistant = crud.add_message(
        db,
        user_id=user_id,
        session_id=session.id,
        role="assistant",
        content=reply.content,
        tool_calls=reply.tool_calls or None,
        token_in=reply.token_in,
        token_out=reply.token_out,
        latency_ms=reply.latency_ms,
    )
    crud.touch_session(db, session, added=2, at=now)

    cost = _estimate_cost(reply.token_in, reply.token_out)
    crud.add_usage_log(
        db,
        user_id=user_id,
        model=reply.model or session.model or settings.llm_model,
        token_in=reply.token_in,
        token_out=reply.token_out,
        cost_estimate=cost,
        latency_ms=reply.latency_ms,
    )
    db.commit()
    db.refresh(assistant)

    return ChatTurn(
        session=session,
        assistant_message=assistant,
        token_in=reply.token_in,
        token_out=reply.token_out,
        latency_ms=reply.latency_ms,
        model=reply.model or session.model or settings.llm_model,
        cost_estimate=cost,
    )


# ---------- 内部 ----------


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

    `chat_session.py:5` 约定的「首条用户消息落库后截前 30 字」就在这里做。
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

    - 第一条是 system（时间 + 分类 + 规则）
    - 只放 user / assistant 且 status=ok 且有正文的消息：
      失败行（status=failed）和工具行（A5 才有）现在放进去只会让模型困惑
    """
    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt(now_beijing(), categories=categories),
        }
    ]
    for row in history:
        if row.status != "ok" or row.role not in ("user", "assistant"):
            continue
        if not (row.content or "").strip():
            continue
        messages.append({"role": row.role, "content": row.content})
    return messages


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
