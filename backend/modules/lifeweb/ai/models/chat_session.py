"""chat_sessions 表（AI 会话）。

设计依据 `docs/数据库设计.md` §3.16。一张表 = 左侧会话列表的一行。

标题策略：新建时默认「新对话」，**首条用户消息落库后**由 A6 截取前 30 字改写
（截断而不是新增字段，避免同一个信息存两份）。

与文档的偏差（有意为之）：`id` / `user_id` 用**有符号 BIGINT**，
与库里已有的 users / tasks 保持一致，避免 signed 与 unsigned 比较时用不上索引。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        # 会话列表的查询永远是「我的、按最后消息时间倒序」
        Index("idx_sessions_user_time", "user_id", "last_message_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 默认值写在库里，应用层漏传也不会变成 NULL
    title: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default="新对话"
    )

    # 本次会话用的模型名，便于事后排查「当时是哪个模型答的」
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 冗余计数：列表页要显示「12 条消息」，不必为此 COUNT 一次子表
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    # 最后一次消息时间：列表排序键。空会话（新建还没说话）为 NULL
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---------- A11：长对话的前情摘要 ----------
    # 摘要只覆盖「已经滑出窗口」的旧消息，第三人称、不超过 300 字。
    # 存成一列而不是一条 system 消息，是因为它是会话的属性（一个会话一份），
    # 而且每次增量更新都是覆盖写，不该在消息列表里堆出多行。
    summary: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT, nullable=True)

    # 摘要已经覆盖到哪条消息（chat_messages.id）。空表示还没摘要过。
    # 下一段要从 summary_until_id 之后开始算，避免同一段被反复摘要。
    summary_until_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # 软删除：有值表示已删，列表查询带 deleted_at IS NULL
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ChatSession id={self.id} title={self.title!r} count={self.message_count}>"
