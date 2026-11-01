"""tasks 表（待办）。

字段与数据库**逐列一致**（就是这 10 列，没有 updated_at / category_id / remind_at）。
以后要加分类、提醒时间，走 Alembic 迁移加列，不在这里预留。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

# 与数据库里的 ENUM 定义严格一致，多一个少一个都会不匹配
TASK_STATUSES = ("todo", "doing", "done", "cancelled")
TASK_PRIORITIES = ("low", "medium", "high")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # 指向 users.id：这条待办属于谁。加索引，因为每条查询都按它过滤
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        Enum(*TASK_STATUSES, name="task_status"),
        nullable=False,
        server_default="todo",
    )
    priority: Mapped[str] = mapped_column(
        Enum(*TASK_PRIORITIES, name="task_priority"),
        nullable=False,
        server_default="medium",
    )

    # 时间一律存 UTC；显示成北京时间在 schema 层做
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # 软删除：有值表示已删，查询时过滤掉
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def is_overdue(self, now_utc: datetime) -> bool:
        """是否逾期：有截止时间、已经过了、且还没完成/取消。

        注意：这是**计算属性**，不落库 —— 落库的话第二天就过期了。
        """
        return (
            self.due_at is not None
            and self.due_at < now_utc
            and self.status in ("todo", "doing")
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Task id={self.id} title={self.title!r} status={self.status}>"
