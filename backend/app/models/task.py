"""待办事项模型。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Index, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin

TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
    "comment": "待办事项",
}

# 未开始 / 进行中 / 已完成 / 已取消
TASK_STATUSES = ("todo", "doing", "done", "cancelled")
TASK_PRIORITIES = ("low", "medium", "high")


class Task(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_user_status_due", "user_id", "status", "due_at"),
        Index("idx_tasks_user_due", "user_id", "due_at"),
        Index("idx_tasks_remind", "remind_at", "status"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="所属用户")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", comment="0=未分类"
    )
    priority: Mapped[str] = mapped_column(
        Enum(*TASK_PRIORITIES, name="task_priority"),
        nullable=False,
        server_default="medium",
    )
    status: Mapped[str] = mapped_column(
        Enum(*TASK_STATUSES, name="task_status"),
        nullable=False,
        server_default="todo",
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="截止时间（UTC）"
    )
    remind_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="提醒时间（UTC）"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="完成时间（UTC）"
    )
    source: Mapped[str] = mapped_column(
        Enum("manual", "ai", name="task_source"),
        nullable=False,
        server_default="manual",
        comment="录入来源：手工或 AI",
    )

    def is_overdue(self, now_utc: datetime) -> bool:
        """是否逾期：有截止时间、已过期、且状态为未开始/进行中。"""
        return (
            self.due_at is not None
            and self.due_at < now_utc
            and self.status in ("todo", "doing")
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Task id={self.id} title={self.title!r} status={self.status}>"
