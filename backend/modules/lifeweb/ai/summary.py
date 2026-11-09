"""A11：长对话的前情摘要（滚动摘要，增量更新）。

问题：`service._build_llm_messages` 只带最近 `HISTORY_LIMIT`(20) 条原文，
第 11 轮之后，开头说过的「我住杭州」「以后记账都算微信」全部丢失 ——
而这恰恰是个人助理最该记住的东西。

做法（滑动窗口 + 滚动摘要）：

    [system 提示词]
    [一条 user 消息：这是之前对话的摘要……]   ← 只在有摘要时出现
    [最近 20 条原文]

三条约定的边界，写在这里免得以后改乱：

1. **只在攒够了才摘要。** 窗口外的旧消息超过 `SUMMARY_TRIGGER` 条才触发；
   每轮都摘要会又贵又慢，而且大部分时候没必要。
2. **增量而不是全量。** 只读「上次摘要之后、本次窗口之前」那一段，
   和旧摘要合并成新摘要，并把 `summary_until_id` 推进到窗口前一条。
   全量重摘要的 token 会随轮数线性增长，增量是常数。
3. **摘要失败不许影响对话。** 任何异常吞掉、记 warning，返回 None，
   调用方照常用最近 20 条原文回答。宁可忘事，不能答不出话。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from modules.lifeweb.ai import crud
from modules.lifeweb.ai.models import ChatMessage, ChatSession
from modules.lifeweb.ai.provider import LLMError, LLMProvider

logger = logging.getLogger(__name__)

# 窗口外的旧消息攒到这么多条，才值得花一次调用去摘要
SUMMARY_TRIGGER = 12

# 一次最多读多少条旧消息来摘要（保险丝，防止补历史时一次读几百条）
SUMMARY_BATCH = 60

# 摘要本身的长度上限（字）。再长就变成「把原文抄了一遍」，失去压缩的意义。
# 200 而不是 300：实测 300 字时模型会顺手把「助理给过什么建议」也写进去，
# 而建议是最不该占位的部分 —— 用户问「我住哪」时，摘要里的番茄炒蛋教程毫无用处。
SUMMARY_MAX_CHARS = 200

# 送给模型的旧消息单条截断长度：一句寒暄也占不了太多 token
_RAW_MAX_CHARS = 200

# 摘要那一次调用的输出上限。中文 200 字大约 300 token，留点余量
_SUMMARY_MAX_TOKENS = 400

SUMMARY_HEADER = "【前情摘要】以下是这次对话更早的内容，仅供你了解背景，不必复述："

_SUMMARY_SYSTEM = """你在为一个「生活助理」应用压缩对话历史。

把用户与助理之前的对话，压缩成一段给助理自己看的前情摘要。

只写「用户自己的事」，一条一条列，每条尽量短：
- 用户的稳定事实：住哪、做什么、家里人/宠物、通勤多久
- 用户明确要求记住的约定：记账默认支付方式、日期习惯、称呼
- 已经办成的事：建过哪些待办、记过哪些账（只写做了什么，不写细节）
- 还没解决的问题：用户问了但没答完的

绝对不要写：
- 助理说过什么、给过什么建议、列过什么清单（那是助理的临时输出，不是用户的处境）
- 寒暄、语气词、被用户推翻的说法、逐句复述
- 「用户说/用户表示/助理建议」这类套话

格式：用第三人称，一段连续文字，不超过 200 字。
如果给了上一版摘要，把新增内容并进去，删掉已经过时或不再重要的条目，不要写成两段，
也不要写「在之前基础上又……」。只输出摘要正文。"""


@dataclass(frozen=True)
class SummaryUpdate:
    """一次摘要生成的结果。"""

    text: str
    until_id: int
    used_old: bool
    token_in: int
    token_out: int
    latency_ms: int
    model: str


def window_start(history: list[ChatMessage]) -> int | None:
    """窗口里最早那条消息的 id。history 为空时返回 None。

    摘要的右边界是「窗口左边界的前一条」，所以调用方要先用它算出 end_id。
    """
    if not history:
        return None
    return history[0].id


def needs_summary(
    db: Session,
    session: ChatSession,
    history: list[ChatMessage],
) -> bool:
    """窗口外是否已经攒够了要摘要的旧消息。

    统计口径是「消息总数 - 窗口里的条数」，不是 id 差值 —— id 是全表自增，
    一个消息多的会话 id 会从几百跳到一千，用 id 相减会把窗口外数量算爆。

    计数是近似的（窗口里的失败行也会被算成「窗口外」），这可以接受：
    触发早了只是多摘要一次，触发晚了只是多带几条原文。
    """
    if not history:
        return False
    total = crud.message_count(db, session.user_id, session.id)
    outside = total - len(history)
    return outside >= SUMMARY_TRIGGER


def _render_history(rows: list[ChatMessage]) -> str:
    """把待摘要的消息渲染成纯文本，跳过失行、工具行、占位行。"""
    lines: list[str] = []
    for row in rows:
        if row.status != "ok" or row.role not in ("user", "assistant"):
            continue
        if row.tool_calls:
            continue
        text = " ".join((row.content or "").split())
        if not text:
            continue
        who = "用户" if row.role == "user" else "助理"
        if len(text) > _RAW_MAX_CHARS:
            text = text[:_RAW_MAX_CHARS] + "…"
        lines.append(f"{who}：{text}")
    return "\n".join(lines)


def build_summary_prompt(old: str | None, rendered: str) -> list[dict]:
    """拼摘要那一次调用的 messages（不带 tools）。"""
    parts: list[str] = []
    if old:
        parts.append(f"上一版摘要：\n{old}")
    parts.append(f"新增对话：\n{rendered}")
    parts.append("请输出合并后的前情摘要。")
    return [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def update_summary(
    db: Session,
    provider: LLMProvider,
    session: ChatSession,
    history: list[ChatMessage],
) -> SummaryUpdate | None:
    """必要时生成一次新摘要并写回 session（只 flush，不 commit）。

    返回 None 表示「这次不需要摘要」或「摘要失败，照常对话」——
    调用方不用区分这两者，两种情况都是「继续往下走」。

    异常一律吞掉：摘要只是锦上添花，不能让它把一条正常消息变成 500。
    """
    if not needs_summary(db, session, history):
        return None

    end_id = window_start(history)
    if end_id is None:
        return None
    end_id -= 1

    rows = crud.messages_upto(
        db,
        session.user_id,
        session.id,
        after_id=session.summary_until_id,
        end_id=end_id,
        limit=SUMMARY_BATCH,
    )
    if not rows:
        return None
    # 实际摘要到哪一条：用读到的最后一条，而不是 end_id ——
    # 命中 batch 上限时两者不同，指针必须停在真读过的地方。
    until_id = rows[-1].id
    rendered = _render_history(rows)
    if not rendered.strip():
        # 这一段全是失败行/工具行，没有可摘要的话：直接推进指针，
        # 否则下一轮还会读到同一批空内容，白跑一次模型
        session.summary_until_id = until_id
        db.flush()
        return None

    old = session.summary or None
    try:
        reply = provider.chat(
            build_summary_prompt(old, rendered),
            temperature=0.2,
            max_tokens=_SUMMARY_MAX_TOKENS,
        )
    except LLMError as exc:
        # 只记 code，不记 message —— message 里可能带着上游返回的原文
        logger.warning("生成前情摘要失败（%s），这一轮用最近的原文照常回答", exc.code)
        return None
    except Exception:  # noqa: BLE001 —— 摘要绝不能把对话带崩
        logger.warning("生成前情摘要时出现意外错误，这一轮用最近的原文照常回答", exc_info=True)
        return None

    text = " ".join((reply.content or "").split())
    if not text:
        logger.warning("模型返回了空摘要，跳过这次更新")
        return None
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS].rstrip() + "…"

    session.summary = text
    session.summary_until_id = until_id
    db.flush()
    return SummaryUpdate(
        text=text,
        until_id=until_id,
        used_old=bool(old),
        token_in=reply.token_in,
        token_out=reply.token_out,
        latency_ms=reply.latency_ms,
        model=reply.model,
    )


def summary_message(text: str | None) -> dict | None:
    """把摘要文本包成一条 user 消息，插在 system 与原文之间。

    为什么是 user 而不是 system：DeepSeek 对「中途出现的 system」比较敏感，
    放成 user 消息、加一个明确的前缀，行为最稳定。
    """
    body = (text or "").strip()
    if not body:
        return None
    return {"role": "user", "content": f"{SUMMARY_HEADER}\n{body}"}


__all__ = [
    "SUMMARY_TRIGGER",
    "SUMMARY_BATCH",
    "SUMMARY_MAX_CHARS",
    "SUMMARY_HEADER",
    "SummaryUpdate",
    "build_summary_prompt",
    "needs_summary",
    "summary_message",
    "update_summary",
    "window_start",
]