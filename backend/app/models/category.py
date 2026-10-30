"""分类字典。

设计要点（见 docs/数据库设计.md §3.2）：
- scope 区分用途：expense/income/task/shopping/diet/exercise/mood
- owner_user_id = 0 表示系统预置；非 0 表示用户自建
- parent_id = 0 表示一级分类，非 0 表示二级子类
- LLM 只能从 is_system=1 且 parent_id=0 的一级分类中选择（防止统计口径碎片化）
"""

from sqlalchemy import Index, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin

TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
    "comment": "分类字典",
}


class Category(TimestampMixin, SoftDeleteMixin, Base):
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
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="用途域：expense/income/task/shopping/diet/exercise/mood",
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_id: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", comment="0=一级分类"
    )
    owner_user_id: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", comment="0=系统预置"
    )
    is_system: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", comment="系统预置不可删"
    )
    icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Category id={self.id} scope={self.scope} name={self.name!r}>"
