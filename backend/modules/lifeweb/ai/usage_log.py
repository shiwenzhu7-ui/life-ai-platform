"""ai_usage_logs 表（AI 调用计量）。

设计依据 `docs/数据库设计.md` §3.20。

每次调模型写一行，两个用途：

1. **配额**：`SELECT COUNT(*) WHERE user_id=? AND created_at >= 今天 00:00`，
   与 `users.daily_ai_quota` 比较（V1.2 引入真实用户后才有意义，现在恒为演示用户）。
2. **成本**：`cost_estimate` 是**估算值**，不是账单。用 token 数 × 单价算出来，
   单价来自配置（`Settings.llm_price_in_per_mtok` / `llm_price_out_per_mtok`，
   元/百万 token），默认 0 表示「只记 token 不估价」，要改只改 `.env`。

和 `chat_messages.token_in/token_out` 的关系：那张表记「这条消息花了多少」，
本表记「这次调用花了多少」。一次用户提问可能触发多次模型调用（工具循环），
所以本表的条数 ≥ 消息条数，统计口径以本表为准。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

USAGE_PURPOSES = ("chat", "embedding", "report")
USAGE_STATUSES = ("ok", "failed")


class UsageLog(Base):
    __tablename__ = "ai_usage_logs"
    __table_args__ = (
        # 当天配额查询：user_id + created_at 范围
        Index("idx_usage_user_time", "user_id", "created_at"),
        # 全局趋势（analytics-web 的用量图）：只要时间
        Index("idx_usage_time", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    purpose: Mapped[str] = mapped_column(
        Enum(*USAGE_PURPOSES, name="usage_purpose"), nullable=False
    )

    model: Mapped[str] = mapped_column(String(64), nullable=False)

    token_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    token_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # 估算费用（元）。6 位小数是为了单价低到 1e-6 时还能记下来
    cost_estimate: Mapped[float] = mapped_column(
        Numeric(10, 6), nullable=False, server_default="0"
    )

    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # 失败也要记：失败率高的时候，配额应该按「尝试次数」算还是「成功次数」算，
    # 数据留着以后才能决定
    status: Mapped[str] = mapped_column(
        Enum(*USAGE_STATUSES, name="usage_status"),
        nullable=False,
        server_default="ok",
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UsageLog id={self.id} purpose={self.purpose} status={self.status}>"
