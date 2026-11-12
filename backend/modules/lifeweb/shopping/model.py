"""购物清单模块（V1.1 先建表，V1.4 才有页面）。

两张表放同一个文件：它们是父子聚合（清单 1—N 明细），
永远一起被读、一起被改，拆成两个文件只会让人多跳一次。

    shopping_lists  一张清单（「周末采购」）
    shopping_items  清单里的一项（牛奶 ×2）

V1.1 阶段这两张表只有**一个写入者**：AI 助手的 `add_shopping_item` 工具
（经用户确认后由 A5 落库）。没有页面，也没有手动增删接口。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

SHOPPING_LIST_STATUSES = ("active", "archived")
SHOPPING_ITEM_SOURCES = ("manual", "ai")


class ShoppingList(Base):
    __tablename__ = "shopping_lists"
    __table_args__ = (
        Index("idx_shopping_lists_user", "user_id", "status"),
        # 同一个人不能有两张同名清单（归档的也算）：AI 的 add_shopping_item
        # 靠名字找清单，重名会让它落到哪张变成随机事件。
        # 加在这里也是为了 alembic check 能认出「模型与库一致」，
        # 漏写会被 autogenerate 报成「要删掉这个索引」。
        UniqueConstraint("user_id", "name", name="uk_shopping_lists_user_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    name: Mapped[str] = mapped_column(String(64), nullable=False)

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # active 在用、archived 归档。归档而不是删除，历史清单还能翻回来
    status: Mapped[str] = mapped_column(
        Enum(*SHOPPING_LIST_STATUSES, name="shopping_list_status"),
        nullable=False,
        server_default="active",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ShoppingList id={self.id} name={self.name!r} status={self.status}>"


class ShoppingItem(Base):
    __tablename__ = "shopping_items"
    __table_args__ = (
        # 打开一张清单：list_id 定位 + 按「买了没」分组显示
        Index("idx_shopping_items_list", "list_id", "is_purchased"),
        # 跨清单统计（「我这个月买了多少东西」）只按用户查
        Index("idx_shopping_items_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # 关联 shopping_lists.id，不加外键（与项目其它表一致）
    list_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 冗余 user_id：明细也常常被直接按用户过滤（AI 工具查「我要买什么」时
    # 只想拿一个 user_id，不想先 join 清单表）
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # 数量：2.5 公斤这种东西存在，所以用 DECIMAL 不用 INT
    quantity: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, server_default="1"
    )

    unit: Mapped[str] = mapped_column(String(16), nullable=False, server_default="个")

    # 关联 categories.id，scope=shopping（食品/日用品/数码/服饰/其他）
    category_id: Mapped[int] = mapped_column(
        mysql.SMALLINT(unsigned=True),
        nullable=False,
        server_default="0",
        comment="关联 categories.id，scope=shopping",
    )

    is_purchased: Mapped[int] = mapped_column(
        mysql.TINYINT(display_width=1), nullable=False, server_default="0"
    )

    # 打勾的时刻：既用于「今天买了什么」的统计，也让排序有依据
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 同一清单内手动排序；AI 追加的项给最大值，排在最后
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # manual = 用户手加；ai = 助手确认后写入（本版本唯一的写入来源）
    source: Mapped[str] = mapped_column(
        Enum(*SHOPPING_ITEM_SOURCES, name="shopping_item_source"),
        nullable=False,
        server_default="manual",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ShoppingItem id={self.id} name={self.name!r} qty={self.quantity}>"
