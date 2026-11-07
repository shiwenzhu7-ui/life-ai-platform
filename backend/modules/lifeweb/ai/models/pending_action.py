"""ai_pending_actions 表（待确认写入）。

设计依据 `docs/数据库设计.md` §3.18。

存在的理由：模型**不允许直接写业务表**。它只能产出一张「草稿」存到这里，
由用户在前端点「确认」之后，才由 A5 的确认接口真正执行 INSERT。
这条规则是 V1.1 的安全底线——模型说「帮你记了 300 元餐饮」，在用户点头之前
账本里不该有任何东西。

状态机（`status`）：

    pending  ──确认──▶ confirmed ──▶ 已写入（executed_at 有值，result 记 task_id 等）
       │
       ├──用户取消──▶ cancelled
       └──超时──▶ expired（expires_at 过期后由清理任务置位）

**只有 pending 能被确认一次**：UPDATE ... WHERE status='pending' 的影响行数就是判据，
两个人同时点确认时只有一个能写成功（幂等的兜底在 idempotency_key 唯一索引）。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

# 与数据库里的 ENUM 定义严格一致。新增动作类型要同时改这里和数据库 ENUM
PENDING_ACTION_TYPES = (
    "create_task",
    "create_expense",
    "add_shopping_item",
    "create_shopping_list",
)
PENDING_STATUSES = ("pending", "confirmed", "cancelled", "expired")


class PendingAction(Base):
    __tablename__ = "ai_pending_actions"
    __table_args__ = (
        # 前端生成 UUID，重复提交时插入直接失败 → 接口把唯一键冲突翻译成「已处理」
        UniqueConstraint("idempotency_key", name="uk_pending_idem"),
        # 「我有哪些待确认的、哪些过期了」——清理任务与前端角标都用它
        Index("idx_pending_user_status", "user_id", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 来源的 assistant 消息。非空：草稿一定是某句话的产物，
    # 没有消息就没有解释「为什么让我写这条」
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    action_type: Mapped[str] = mapped_column(
        Enum(*PENDING_ACTION_TYPES, name="pending_action_type"), nullable=False
    )

    # 已通过 Schema 校验的结构化草稿，形如
    # {"title": "买牛奶", "priority": "medium", "due_at": "2026-11-07T10:00:00+08:00"}
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    status: Mapped[str] = mapped_column(
        Enum(*PENDING_STATUSES, name="pending_status"),
        nullable=False,
        server_default="pending",
    )

    # UUID 字符串。文档写 CHAR(36)，这里用 VARCHAR(36)：
    # 内容恒为 36 字符，CHAR 在 utf8mb4 下会按 36×4 字节定长占位，
    # VARCHAR 只占实际长度 + 1，唯一索引也更小
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)

    # 默认 +30 分钟由应用层算好再写，不用数据库的 NOW() + INTERVAL，
    # 因为库里存的是 UTC，与应用层时区口径必须一致
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 执行结果，如 {"task_id": 12}：前端据此跳转到刚创建的记录
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PendingAction id={self.id} type={self.action_type} status={self.status}>"

