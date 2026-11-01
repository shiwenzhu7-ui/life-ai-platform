"""categories 表（分类字典）。

字段按 docs/数据库设计.md §3.2 定义，与数据库逐列一致。

设计要点：
  scope          用途域：expense / income / task / shopping / diet / exercise / mood
  owner_user_id  0 = 系统预置；非 0 = 用户自建（BIGINT，因为要存 users.id）
  parent_id      0 = 一级分类；非 0 = 二级子类
  is_system      系统预置不可删
  LLM 只能从 is_system=1 且 parent_id=0 的一级分类里选，防止统计口径碎片化

类型为什么用 mysql.SMALLINT(unsigned=True) 而不是 SmallInteger：
  文档要求 SMALLINT UNSIGNED。带符号的 SmallInteger 上限只有 32767，
  而且和库里实际的列定义不一致会让 `alembic check` 一直报 diff。
"""

from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

# 与数据库里的 ENUM 定义严格一致，多一个少一个都会不匹配
CATEGORY_SCOPES = ("expense", "income", "task", "shopping", "diet", "exercise", "mood")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "owner_user_id",
            "parent_id",
            "name",
            name="uk_categories_scope_owner_parent_name",
        ),
        Index("idx_categories_scope_sort", "scope", "sort_order"),
    )

    id: Mapped[int] = mapped_column(
        mysql.SMALLINT(unsigned=True), primary_key=True, autoincrement=True
    )

    scope: Mapped[str] = mapped_column(
        Enum(*CATEGORY_SCOPES, name="category_scope"),
        nullable=False,
        comment="用途域：expense/income/task/...",
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)

    parent_id: Mapped[int] = mapped_column(
        mysql.SMALLINT(unsigned=True),
        nullable=False,
        server_default="0",
        comment="0=一级分类",
    )
    owner_user_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        nullable=False,
        server_default="0",
        comment="0=系统预置",
    )
    is_system: Mapped[int] = mapped_column(
        mysql.TINYINT(display_width=1),
        nullable=False,
        server_default="0",
        comment="1=系统预置不可删",
    )
    icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", comment="同 scope 内的展示顺序，从 1 开始"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="软删除时间，NULL 表示未删除"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Category id={self.id} scope={self.scope} name={self.name!r}>"
