"""收支记录模型。

V1.0 只建表（为迁移完整性），接口在 V1.0 第 2 步实现。
注意：表名是 expenses，但通过 type 区分支出与收入。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, DateTime, Enum, Index, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin

TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
    "comment": "收支记录",
}


class Expense(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "expenses"
    __table_args__ = (
        Index("idx_expenses_user_time", "user_id", "occurred_at"),
        Index("idx_expenses_user_cat_time", "user_id", "category_id", "occurred_at"),
        Index("idx_expenses_user_type_time", "user_id", "type", "occurred_at"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(
        Enum("expense", "income", name="expense_type"),
        nullable=False,
        server_default="expense",
        comment="支出 / 收入",
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, comment="金额，恒为正数，方向由 type 表达"
    )
    category_id: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="关联 categories.id"
    )
    account: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="现金", comment="现金/微信/支付宝/银行卡"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="发生时间（UTC），统计按此字段"
    )
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(
        Enum("manual", "ai", name="expense_source"),
        nullable=False,
        server_default="manual",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Expense id={self.id} type={self.type} amount={self.amount}>"
