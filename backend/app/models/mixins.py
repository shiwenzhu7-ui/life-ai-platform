"""ORM 通用字段 Mixin。

约定（见 docs/数据库设计.md §1）：
- 时间统一存 naive UTC，应用层用 app.core.time 转换
- 个人业务表统一软删除：deleted_at IS NULL 表示未删除
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """创建/更新时间。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="创建时间（UTC）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间（UTC）",
    )


class SoftDeleteMixin:
    """软删除标记。"""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        default=None,
        comment="软删除时间（UTC），NULL 表示未删除",
    )
