"""chat_messages 表（AI 消息）。

设计依据 `docs/数据库设计.md` §3.17。一行 = 对话里的一句话（或一次工具往返）。

四种 role 的分工，决定了「一条对话存成几行」：

    user      用户说的话（content 有值）
    assistant 模型的回复（content 可能有值；也可能 content 为空、只有 tool_calls）
    tool      工具执行结果（tool_result 有值，content 通常为空）
    system    系统提示词（一般只在会话开头一条，便于复现当时的行为）

`token_in` / `token_out` / `latency_ms` 记在这一行上，是为了「这条回复花了多少」能直接查；
汇总到 `ai_usage_logs` 的是**按次调用**的账，两者粒度不同，都留着。
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Enum, Index, Integer, String, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

# 与数据库里的 ENUM 定义严格一致
CHAT_ROLES = ("user", "assistant", "system", "tool")
CHAT_MESSAGE_STATUSES = ("ok", "failed")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        # 取某会话的消息：session_id 定位 + id 升序即时间序（自增 id 天然单调，
        # 不需要再按 created_at 排，也不会因为同一秒两条而错序）
        Index("idx_messages_session", "session_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # 冗余 user_id：鉴权时只查这一列就能挡掉「别人的会话」，
    # 不必先查 chat_sessions 再比对
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 关联 chat_sessions.id，不加外键（与项目其它表一致），存在性由应用层保证
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    role: Mapped[str] = mapped_column(Enum(*CHAT_ROLES, name="chat_role"), nullable=False)

    # 助手的回复可能很长，用 MEDIUMTEXT（16MB）而不是 TEXT（64KB）
    content: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT, nullable=True)

    # 模型发起的工具调用原始数组，形如
    # [{"id": "call_1", "name": "query_tasks", "arguments": {...}}]
    # 原样存下来，重放对话时才可能复现当时的行为
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # role=tool 时工具返回的内容
    tool_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    token_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    token_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # 调用失败也要留一行（failed），否则「为什么没回我」永远查不出来
    status: Mapped[str] = mapped_column(
        Enum(*CHAT_MESSAGE_STATUSES, name="chat_message_status"),
        nullable=False,
        server_default="ok",
    )
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ChatMessage id={self.id} role={self.role} session={self.session_id}>"
