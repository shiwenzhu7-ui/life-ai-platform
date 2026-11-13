"""管理后台的分级权限（V1.4）。

角色只有三种（`users.role` 的 ENUM：user / vip / admin），但**管理员分两级**，
区别不在 role 字段上，而在「这个 admin 是不是根管理员」：

    第零级  服务器 / .env          —— 能设根管理员（改库、改配置），不在后台界面里
    第一级  根管理员（root）        —— 能改任何人的任何身份；自己不可被任何人改动
    第二级  普通管理员（admin）     —— **只能在普通用户和 VIP 之间来回改**，外加启用/禁用；但：

- 不能碰根管理员（`ROOT_PROTECTED`）
- 不能把别人提成 admin（`CANNOT_GRANT_ADMIN`：只有根管理员能授予）
- 不能改管理员（无论哪一级），包括撤销他的 admin、启用/禁用（`CANNOT_TOUCH_ADMIN`）

根管理员是谁：`settings.admin_root_user_id` 配了就用它；没配就取 users 表里
**id 最小的 role=admin**。后者让「老库里只有 demo 一个 admin」这个现状天然成立，
不用改 .env；代价是「把另一个更早的 id 提成 admin」会悄悄换掉根 ——
所以一旦定下来，建议在 .env 里钉死 `ADMIN_ROOT_USER_ID`。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import settings
from core.errors import validation_error
from modules.shared.users.model import User

ADMIN = "admin"


def root_admin_id(db: Session) -> int | None:
    """根管理员的 user_id；库里没有可用根管理员时返回 None。

    只看 active 的 admin：一个被禁用的根等于没有根，这时后台应当只能改普通用户，
    不能出现「谁都能当根」的兜底。
    """
    if settings.is_root_admin_configured:
        return settings.admin_root_user_id
    return db.execute(
        select(User.id)
        .where(User.role == ADMIN, User.status == "active", User.deleted_at.is_(None))
        .order_by(User.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def is_root(db: Session, user: User | None) -> bool:
    if user is None:
        return False
    return root_admin_id(db) == user.id


def check_can_update(db: Session, *, actor: User, target: User, role: str | None, status: str | None) -> None:
    """改角色 / 改状态之前的分级闸门。不通过就抛 422。"""

    if target.id == actor.id:
        if role is not None and role != ADMIN:
            raise validation_error("不能修改自己的角色，请让其他管理员操作")
        if status is not None and status != "active":
            raise validation_error("不能禁用自己")

    actor_is_root = is_root(db, actor)

    # 1) 根管理员谁都动不了 —— 包括他自己（上面那条已经挡了「自己改自己」）。
    #    想换根，只能到服务器上改库或改 .env，这是有意的：界面上永远没有一个
    #    能一键把自己和所有人都锁死的按钮。
    if is_root(db, target) and target.id != actor.id:
        raise validation_error(
            "这是根管理员，任何人都不能修改他的角色或状态；要换人请由运维在服务器上处理",
            code="ROOT_PROTECTED",
        )

    if actor_is_root:
        return

    # 2) 以下是「普通管理员」的限制。他对普通用户和 VIP 有完整权限
    #    （user ↔ vip 互转、启用/禁用），但完全不碰管理员这一层。
    if target.role == ADMIN:
        raise validation_error(
            "只有根管理员可以修改管理员账号", code="CANNOT_TOUCH_ADMIN"
        )

    if role == ADMIN:
        raise validation_error(
            "只有根管理员可以把别人设为管理员；你只能在普通用户和 VIP 之间调整",
            code="CANNOT_GRANT_ADMIN",
        )