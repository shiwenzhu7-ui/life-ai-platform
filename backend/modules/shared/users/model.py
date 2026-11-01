"""users 表。

字段与当前数据库**逐列一致**（V1.0 只用到 id / username / nickname / created_at）。
V1.2 加登录时才需要 password_hash / role / status 等列，那时走迁移加，不在这里预留。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # 用户名：不能为空、不能重复
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)

    # 显示名：可以为空
    nickname: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 注册时间：由数据库自动填（Python 不用管）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} username={self.username!r}>"
