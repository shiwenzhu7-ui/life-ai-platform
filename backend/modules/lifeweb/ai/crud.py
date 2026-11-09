"""AI 会话的数据访问层 —— 只写 SQL，不写业务规则。

两条铁律（每个查询都必须满足，和 tasks/crud.py 一致）：

  1. 必须带 user_id 过滤 —— 别人的会话查不到，对外是 404 而不是 403
  2. 会话查询必须排除软删除（deleted_at IS NULL）

事务边界（A4 起）：
  会话 CRUD（新建 / 改标题 / 软删）仍是单表写，在本层 commit；
  「写消息 + 改会话计数 + 记用量」是一次改多张表，由 `service.py` 统一提交，
  所以 `add_message` / `touch_session` / `add_usage_log` 只 flush、不 commit ——
  谁开的事务谁提交，半途 commit 会把「要么全成要么全不成」拆成两段。
"""

from datetime import datetime
from typing import Literal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from core.time import now_utc
from modules.lifeweb.ai.models import (
    ChatMessage,
    ChatSession,
    PendingAction,
    UsageLog,
)
from modules.lifeweb.ai.schema import DEFAULT_TITLE
from modules.shared.categories.model import Category


def _session_base(user_id: int) -> Select:
    """会话查询的公共起点：限本人 + 未删除。"""
    return select(ChatSession).where(
        ChatSession.user_id == user_id,
        ChatSession.deleted_at.is_(None),
    )


def list_sessions(
    db: Session, user_id: int, *, page: int = 1, size: int = 20
) -> tuple[list[ChatSession], int]:
    """当前用户的会话列表，返回 (当页数据, 总数)。

    排序：最近说过话的在最上面。

    1. `last_message_at.is_(None)` 作为第一键升序 —— MySQL 没有 NULLS LAST，
       用布尔值把空会话（新建还没说话）压到最下面；非空是 0、空是 1，升序即「非空在前」。
    2. `last_message_at.desc()` —— 时间越近越靠前。
    3. `id.desc()` 兜底 —— 同一秒建的两个会话顺序才稳定，翻页不会重复或漏。

    total 在分页之前算，否则拿到的是「当页条数」。
    """
    base = _session_base(user_id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    stmt = (
        base.order_by(
            ChatSession.last_message_at.is_(None),
            ChatSession.last_message_at.desc(),
            ChatSession.id.desc(),
        )
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(db.scalars(stmt).all()), int(total)


def get_session(db: Session, user_id: int, session_id: int) -> ChatSession | None:
    """按 ID 取一个会话。

    不属于该用户、或已软删，一律返回 None —— 对外统一 404。
    返回 403 等于告诉对方「这个 ID 存在，只是不是你的」，能被用来探测别人的数据量。
    """
    return db.scalar(_session_base(user_id).where(ChatSession.id == session_id))


def create_session(
    db: Session,
    user_id: int,
    *,
    title: str = DEFAULT_TITLE,
    model: str | None = None,
) -> ChatSession:
    """新建会话。

    三个要点：
      1. user_id 由服务端给，不读请求体
      2. message_count 用库默认值 0，这里不显式写 —— 少一处能写错的地方
      3. commit 之后必须 refresh：created_at / updated_at 是数据库默认值，
         不 refresh 拿回来是 None，响应里会缺字段
    """
    session = ChatSession(user_id=user_id, title=title, model=model)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def update_title(db: Session, session: ChatSession, title: str) -> ChatSession:
    """改标题。

    updated_at 的刷新靠模型上的 `onupdate=func.now()`，这里不用手写 ——
    手写容易忘，忘了列表排序和「最近改动」就都不准。
    """
    session.title = title
    db.commit()
    db.refresh(session)
    return session


def soft_delete_session(db: Session, session: ChatSession) -> None:
    """软删会话：只写 deleted_at，不执行 DELETE。

    消息不跟着删：会话藏起来就够了，消息留着才能回答
    「上周问过什么」「这个月 AI 花了多少」这类事后问题。
    列表和详情都按 deleted_at IS NULL 过滤，前端看不到它们。

    不返回对象：接口回 204，没有响应体。
    """
    session.deleted_at = now_utc()
    db.commit()


def list_messages(
    db: Session,
    user_id: int,
    session_id: int,
    *,
    page: int = 1,
    size: int = 50,
    order: Literal["asc", "desc"] = "asc",
) -> tuple[list[ChatMessage], int]:
    """某会话的消息，按发生顺序（id 升序）返回。

    `order="desc"` 时改成「最新在前」，page=1 就是最近 N 条 ——
    聊天窗口要的就是这个：不能翻开是三个月前的开场白。
    前端拿到 desc 结果后反转显示，就能保持「旧 → 新」的视觉顺序。

    为什么按 id 而不是 created_at：自增 id 天然单调，
    同一秒内的两条消息也不会因为时间戳相同而错序。

    这里不再校验会话是否存在/是否软删 —— 调用方（router）先用
    get_session 挡过一道，挡不住就说明这层被绕过用了。

    user_id 冗余过滤：chat_messages 自己带 user_id（models/chat_message.py:41），
    多这一条条件，即使 session_id 被猜中也读不到别人的消息。
    """
    conds = [
        ChatMessage.user_id == user_id,
        ChatMessage.session_id == session_id,
    ]
    base = select(ChatMessage).where(*conds)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    direction = ChatMessage.id.desc() if order == "desc" else ChatMessage.id.asc()
    stmt = base.order_by(direction).offset((page - 1) * size).limit(size)
    return list(db.scalars(stmt).all()), int(total)


def pending_by_message(
    db: Session, user_id: int, message_ids: list[int]
) -> dict[int, PendingAction]:
    """这一页消息里，哪些产生过待确认卡片 —— `{message_id: PendingAction}`。

    **一次查完**，不在列表循环里逐条查：50 条消息就是 50 次往返，
    消息列表是聊天页每次打开都要调的位置，不能这么花。

    不按 status 过滤：已确认 / 已取消 / 已过期的也要带回去 ——
    卡片刷新后应该显示成「已创建」而不是消失。

    一张助手消息目前最多挂一张卡（`MAX_DRAFTS_PER_REPLY=1`），所以直接按
    message_id 建字典；若将来放宽到多张，这里要改成 `dict[int, list[PendingAction]]`
    （按 id 升序覆盖，最后留下的是最新那张）。
    """
    if not message_ids:
        return {}
    rows = db.scalars(
        select(PendingAction)
        .where(
            PendingAction.user_id == user_id,
            PendingAction.message_id.in_(message_ids),
        )
        .order_by(PendingAction.id.asc())
    ).all()
    return {row.message_id: row for row in rows}


# ---------- A4：发消息走写路径 ----------


def recent_messages(
    db: Session, user_id: int, session_id: int, *, limit: int = 20
) -> list[ChatMessage]:
    """取最近 limit 条消息，**按发生顺序（旧 → 新）返回**。

    实现是「先倒序取 N 条，再反转」：直接正序 LIMIT 拿到的是最早的 N 条，
    对话一长，模型就只记得开场那几句、忘了刚才说什么。

    user_id 冗余过滤：和 list_messages 同一个理由 —— session_id 猜不中，
    猜中了也读不到别人的消息。
    """
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.user_id == user_id,
            ChatMessage.session_id == session_id,
        )
        .order_by(ChatMessage.id.desc())
        .limit(limit)
    )
    return list(reversed(list(db.scalars(stmt).all())))


def messages_upto(
    db: Session,
    user_id: int,
    session_id: int,
    *,
    after_id: int | None,
    end_id: int,
    limit: int,
) -> list[ChatMessage]:
    """取 (after_id, end_id] 区间里的消息，按 id 升序、最多 limit 条。

    A11 摘要用：after_id 是上次摘要覆盖到的位置（None 表示从头），
    end_id 是本次窗口左边界的前一条 —— 窗口之内的消息要原样发给模型，
    不能被折叠掉。`limit` 是保险丝，防止一次读几百条。
    """
    stmt = select(ChatMessage).where(
        ChatMessage.user_id == user_id,
        ChatMessage.session_id == session_id,
        ChatMessage.id <= end_id,
    )
    if after_id is not None:
        stmt = stmt.where(ChatMessage.id > after_id)
    stmt = stmt.order_by(ChatMessage.id.asc()).limit(limit)
    return list(db.scalars(stmt).all())


def message_count(db: Session, user_id: int, session_id: int) -> int:
    """这个会话一共多少条消息（含失败行、assistant 空行）。

    只用来判断「窗口外是否攒够了一次摘要」，不需要精确到语义层面。
    """
    stmt = (
        select(func.count())
        .select_from(ChatMessage)
        .where(
            ChatMessage.user_id == user_id,
            ChatMessage.session_id == session_id,
        )
    )
    return int(db.scalar(stmt) or 0)


def add_message(
    db: Session,
    *,
    user_id: int,
    session_id: int,
    role: str,
    content: str | None = None,
    tool_calls: list | None = None,
    tool_result: dict | None = None,
    token_in: int = 0,
    token_out: int = 0,
    latency_ms: int = 0,
    status: str = "ok",
    error_message: str | None = None,
) -> ChatMessage:
    """插一条消息，flush 拿到自增 id，**不 commit**。

    failed 消息也走这里：模型挂了也要留一行，否则用户刷新页面会看到
    「我明明问过，怎么没了」。error_message 由列定义限长 500，调用方负责截。
    """
    message = ChatMessage(
        user_id=user_id,
        session_id=session_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_result=tool_result,
        token_in=token_in,
        token_out=token_out,
        latency_ms=latency_ms,
        status=status,
        error_message=error_message,
    )
    db.add(message)
    db.flush()
    return message


def touch_session(
    db: Session,
    session: ChatSession,
    *,
    added: int = 0,
    at: datetime | None = None,
) -> ChatSession:
    """一问一答后更新会话：消息数 +added、最后消息时间 = at。**不 commit**。

    added 传 2（用户 + 助手），失败时也是 2 —— 失败的那条也占一行，
    计数和 `GET /messages` 返回的条数必须对得上，否则前端分页会少一页。
    """
    session.message_count = (session.message_count or 0) + added
    session.last_message_at = at or now_utc()
    db.flush()
    return session


def add_usage_log(
    db: Session,
    *,
    user_id: int,
    model: str,
    purpose: str = "chat",
    token_in: int = 0,
    token_out: int = 0,
    cost_estimate: float = 0.0,
    latency_ms: int = 0,
    status: str = "ok",
    error_code: str | None = None,
) -> UsageLog:
    """记一次模型调用的流水。**不 commit**。

    失败也记：失败率高的时候，「配额按尝试算还是按成功算」得靠这些数据才能定。
    """
    log = UsageLog(
        user_id=user_id,
        purpose=purpose,
        model=model,
        token_in=token_in,
        token_out=token_out,
        cost_estimate=cost_estimate,
        latency_ms=latency_ms,
        status=status,
        error_code=error_code,
    )
    db.add(log)
    db.flush()
    return log


def system_categories(db: Session) -> dict[str, list[str]]:
    """系统预置的一级分类，按用途域分组：{scope: [名字, ...]}。

    过滤条件与「LLM 只能选系统一级分类」的约定一致（categories/model.py:8）：
    is_system=1、parent_id=0、owner_user_id=0、未软删。

    只取 A4 提示词用得上的四个域；diet/exercise/mood 不取，因为还没有对应工具，
    让模型看见它们只会诱导它编造「已记录饮食」。
    """
    scopes = ("task", "expense", "income", "shopping")
    stmt = (
        select(Category.scope, Category.name)
        .where(
            Category.is_system == 1,
            Category.owner_user_id == 0,
            Category.parent_id == 0,
            Category.deleted_at.is_(None),
            Category.scope.in_(scopes),
        )
        .order_by(Category.scope, Category.sort_order)
    )
    grouped: dict[str, list[str]] = {}
    for scope, name in db.execute(stmt).all():
        grouped.setdefault(scope, []).append(name)
    return grouped
