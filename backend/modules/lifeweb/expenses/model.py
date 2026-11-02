"""expenses 表（收支记录）。

字段与数据库**逐列一致**（13 列），设计依据 `docs/数据库设计.md` §3.4。

一处与文档的偏差（有意为之）：文档写 `BIGINT UNSIGNED`，这里用**有符号 BIGINT**，
因为库里已有的 users / tasks 都是 `bigint`（当初 create_all 建的），
让新表跟着现实走，避免 unsigned 与 signed 列比较时用不上索引。

三条索引全部以 user_id 打头，因为**每一条查询都按用户隔离**：
    idx_expenses_user_time        (user_id, occurred_at)              列表默认排序
    idx_expenses_user_cat_time    (user_id, category_id, occurred_at) 按分类筛选
    idx_expenses_user_type_time   (user_id, type, occurred_at)        按支出/收入筛选
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Index,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

# 与数据库里的 ENUM 定义严格一致
EXPENSE_TYPES = ("expense", "income")
EXPENSE_SOURCES = ("manual", "ai")


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        Index("idx_expenses_user_time", "user_id", "occurred_at"),
        Index("idx_expenses_user_cat_time", "user_id", "category_id", "occurred_at"),
        Index("idx_expenses_user_type_time", "user_id", "type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 方向：支出还是收入。金额恒为正数，方向由这个字段表达
    type: Mapped[str] = mapped_column(
        Enum(*EXPENSE_TYPES, name="expense_type"),
        nullable=False,
        server_default="expense",
    )

    # 钱一律 DECIMAL，绝不用 float：float 存 0.1 会变成 0.10000000149...
    # 12 位总长、2 位小数，够到 9999999999.99；业务上限 9999999.99 由 schema 层校验
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)

    # 关联 categories.id：type=expense 时必须在 expense 域，income 时在 income 域。
    # 同样不加外键，存在性与域匹配由应用层校验（见 router 的 _check_category）
    category_id: Mapped[int] = mapped_column(
        mysql.SMALLINT(unsigned=True),
        nullable=False,
        comment="关联 categories.id，域与 type 一致",
    )

    # 现金 / 微信 / 支付宝 / 银行卡
    account: Mapped[str] = mapped_column(String(32), nullable=False, server_default="现金")

    # 发生时间（什么时候花的钱），不是录入时间；统计一律按它分组。存 UTC
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # 标签数组，如 ["聚餐", "同事"]；上限 10 个由 schema 层校验
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # manual = 用户手记；ai = 助手确认后写入（V1.1）
    source: Mapped[str] = mapped_column(
        Enum(*EXPENSE_SOURCES, name="expense_source"),
        nullable=False,
        server_default="manual",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    # 用 onupdate 在 ORM 更新时刷新（应用层时间），不依赖数据库的 ON UPDATE
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # 软删除：有值表示已删
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Expense id={self.id} type={self.type} amount={self.amount} "
            f"occurred_at={self.occurred_at}>"
        )
