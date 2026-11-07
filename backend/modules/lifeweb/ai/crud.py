"""AI 会话的数据访问层 —— 只写 SQL，不写业务规则。

两条铁律（每个查询都必须满足，和 tasks/crud.py 一致）：

  1. 必须带 user_id 过滤 —— 别人的会话查不到，对外是 404 而不是 403
  2. 会话查询必须排除软删除（deleted_at IS NULL）

写操作在本层 commit：现在都是单表写入，事务边界就在一条语句里。
等 A4 出现「写消息 + 改会话消息数 + 记用量」这种一次改多张表的操作，
再把事务边界上提到 service 层 —— 现在提前加一层只会多一个「谁负责提交」的含糊地带。
"""

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from core.time import now_utc
from modules.lifeweb.ai.chat_message import ChatMessage
from modules.lifeweb.ai.chat_session import ChatSession
from modules.lifeweb.ai.schema import DEFAULT_TITLE


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
) -> tuple[list[ChatMessage], int]:
    """某会话的消息，按发生顺序（id 升序）返回。

    为什么按 id 而不是 created_at：自增 id 天然单调，
    同一秒内的两条消息也不会因为时间戳相同而错序。

    这里不再校验会话是否存在/是否软删 —— 调用方（router）先用
    get_session 挡过一道，挡不住就说明这层被绕过用了。

    user_id 冗余过滤：chat_messages 自己带 user_id（chat_message.py:41），
    多这一条条件，即使 session_id 被猜中也读不到别人的消息。
    """
    conds = [
        ChatMessage.user_id == user_id,
        ChatMessage.session_id == session_id,
    ]
    base = select(ChatMessage).where(*conds)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.order_by(ChatMessage.id.asc()).offset((page - 1) * size).limit(size)
    return list(db.scalars(stmt).all()), int(total)
