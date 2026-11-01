"""categories 表（分类字典）。

⚠️ 这张表**还没在数据库里创建**。字段按 docs/数据库设计.md §3.2 定义，
等 V1.0 建表时用 Alembic 迁移（或临时 `Base.metadata.create_all(engine)`）建出来。

设计要点：
  scope          用途域：expense / income / task / shopping / diet / exercise / mood
  owner_user_id  0 = 系统预置；非 0 = 用户自建
  parent_id      0 = 一级分类；非 0 = 二级子类
  is_system      系统预置不可删
  LLM 只能从 is_system=1 且 parent_id=0 的一级分类里选，防止统计口径碎片化
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, SmallInteger, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


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

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)

    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="用途域：expense/income/task/..."
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)

    parent_id: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", comment="0=一级分类"
    )
    owner_user_id: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", comment="0=系统预置"
    )
    is_system: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", comment="1=系统预置不可删"
    )
    icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")

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
