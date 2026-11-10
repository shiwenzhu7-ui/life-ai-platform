"""users 表。

V1.0：只用 id / username / nickname / created_at 四列。
V1.2：一次性补成「JWT 用户系统」需要的全部列（迁移 b1users）。

为什么现在就把 role / status / keycloak_sub 一起建好而不是用到再加：
    每次 ALTER TABLE 都要面对「库里已经有数据」的旧行怎么填默认值、
    历史迁移能不能在别人的机器上重放。字段一次建全、功能分版本启用
    （见 docs/项目规划.md §3）时，后者只需要加代码，不需要再动表结构。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

# ENUM 的取值列表要和迁移里写的**逐字一致**（顺序也一致）：
# MySQL 存的是编号，两边顺序不同时读出来的值会错位。
AUTH_SOURCES = ("local", "keycloak")
# vip 于 V2.0 启用；现在建全是为了后面不用再改 ENUM
ROLES = ("user", "vip", "admin")
STATUSES = ("active", "disabled")


class User(Base):
    __tablename__ = "users"

    # 管理员后台按角色筛用户（docs/数据库设计.md:71）。建在模型上，
    # 迁移里那份同名索引才不会在下次 autogenerate 时被当成「多余」删掉。
    __table_args__ = (Index("idx_users_role", "role"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # 用户名：不能为空、不能重复
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)

    # ---------- V1.2 新增 ----------
    email: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)

    # 显示名：可以为空
    nickname: Mapped[str | None] = mapped_column(String(50), nullable=True)

    avatar_url: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # bcrypt 哈希；V2.0 由 Keycloak 管密码后这一列会留空
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    auth_source: Mapped[str] = mapped_column(
        Enum(*AUTH_SOURCES, name="auth_source"), nullable=False, server_default="local"
    )

    # V2.0：Keycloak Token 的 sub
    keycloak_sub: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)

    role: Mapped[str] = mapped_column(
        Enum(*ROLES, name="user_role"), nullable=False, server_default="user"
    )

    # disabled 的用户即使密码正确也不许登录（403 USER_DISABLED）
    status: Mapped[str] = mapped_column(
        Enum(*STATUSES, name="user_status"), nullable=False, server_default="active"
    )

    # 每日 AI 调用额度。真正拦截是后续版本的事，这里先把口径建好
    daily_ai_quota: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("50")
    )

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 注册时间：由数据库自动填（Python 不用管）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # updated_at：MySQL 自己维护，不用在 Python 里赋值
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        server_onupdate=func.now(),
        nullable=False,
    )

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} username={self.username!r} role={self.role!r}>"