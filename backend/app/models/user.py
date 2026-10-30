"""用户模型。

V1.0 只用 id / username / role 三个字段（固定演示用户 id=1）；
password_hash、auth_source、keycloak_sub 为 V1.2 / V2.0 预留，**字段一次建全，避免后续 ALTER TABLE**。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin

TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
    "comment": "用户",
}


class User(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"
    __table_args__ = (TABLE_ARGS,)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    nickname: Mapped[str | None] = mapped_column(String(50), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # V1.2 本地密码；V2.0 起由 Keycloak 管理，此字段为空
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_source: Mapped[str] = mapped_column(
        Enum("local", "keycloak", name="user_auth_source"),
        nullable=False,
        server_default="local",
        comment="账号来源",
    )
    keycloak_sub: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, comment="Keycloak Token 的 sub"
    )

    role: Mapped[str] = mapped_column(
        Enum("user", "vip", "admin", name="user_role"),
        nullable=False,
        server_default="user",
        index=True,
    )
    status: Mapped[str] = mapped_column(
        Enum("active", "disabled", name="user_status"),
        nullable=False,
        server_default="active",
    )
    daily_ai_quota: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="50", comment="每日 AI 调用额度"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} username={self.username!r} role={self.role}>"
