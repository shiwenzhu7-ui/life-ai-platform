"""管理后台接口（V1.4）：用户管理 + AI 调用明细 + 角色说明。

**整个 router 挂 `Depends(require_role("admin"))`**，不是在每个函数体里写 if ——
新增接口时忘了加权限，后果是越权；挂在 router 上就只能在定义时忘，改起来一眼能看见。

路径：`/api/v1/admin/users`、`/api/v1/admin/usage`、`/api/v1/admin/roles`。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import require_role
from modules.adminweb.users import crud
from modules.adminweb.users.permission import is_root, root_admin_id
from modules.adminweb.users.schema import (
    ROLE_CATALOG,
    AdminRole,
    AdminStatus,
    AdminUserOut,
    AdminUserUpdate,
    RoleOut,
    UsageOut,
    UsagePageOut,
    UsageSummaryOut,
)
from modules.shared.pagination import Page
from modules.shared.users.model import User

router = APIRouter(
    prefix="/admin",
    tags=["管理后台"],
    dependencies=[Depends(require_role("admin"))],
)


@router.get("/users", response_model=Page[AdminUserOut], summary="用户列表")
def list_users(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=50, description="用户名/昵称/邮箱"),
    role: AdminRole | None = Query(default=None),
    status: AdminStatus | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Page[AdminUserOut]:
    """所有用户，按注册时间倒序。`usage_count` 是累计 AI 调用次数。

    `is_root_admin` 标出第一级根管理员：前端据此锁住那一行的编辑按钮。
    """
    rows, total = crud.list_users(
        db, page=page, size=size, keyword=keyword, role=role, status=status
    )
    root_id = root_admin_id(db)
    items = [
        AdminUserOut.from_model(user, usage_count=count, is_root=(user.id == root_id))
        for user, count in rows
    ]
    return Page.create(items, total, page, max(1, min(size, 100)))


@router.patch("/users/{user_id}", response_model=AdminUserOut, summary="改角色或禁用")
def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    actor: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> AdminUserOut:
    """改 `role` 或 `status`，两个字段至少要给一个。

    分级规则（详见 `modules/adminweb/users/permission.py`）：

        - 根管理员（库里 id 最小的 admin，或 .env 钉死的那个）谁都不能动
        - 只有根管理员能把别人设为管理员，也只有他能改管理员
        - 普通管理员只能改普通用户 / VIP
        - 谁都不能改自己

    错误码：

        404  用户不存在
        422  没有需要修改的字段 / 改自己 / 违反上面的分级规则
             （`code` 细分：ROOT_PROTECTED、CANNOT_GRANT_ADMIN、CANNOT_TOUCH_ADMIN）

    为什么不开放改密码、改昵称：白名单只认 role 和 status。
    管理员替人改密码属于高风险操作，需要单独设计。
    """
    target = crud.get_user(db, user_id)
    updated = crud.update_user(
        db, target, role=payload.role, status=payload.status, actor=actor
    )
    counts = crud.usage_counts(db)
    return AdminUserOut.from_model(
        updated, usage_count=counts.get(updated.id, 0), is_root=is_root(db, updated)
    )


@router.get("/usage", response_model=UsagePageOut, summary="AI 调用明细")
def list_usage(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user_id: int | None = Query(default=None, ge=1),
    keyword: str | None = Query(default=None, max_length=50, description="用户名或用户 ID"),
    start: datetime | None = Query(default=None, description="起始时间（含）"),
    end: datetime | None = Query(default=None, description="结束时间（不含）"),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> UsagePageOut:
    """按时间倒序列出每次模型调用，并带筛选范围内的合计。

    合计放在响应里是为了回答「今天花了多少」时不用再请求一次；翻页时合计不变。
    """
    rows, total, summary = crud.list_usage(
        db,
        page=page,
        size=size,
        user_id=user_id,
        keyword=keyword,
        start=start,
        end=end,
        status=status,
    )
    page_size = max(1, min(size, 100))
    items = [UsageOut.from_model(row, username=username) for row, username in rows]
    return UsagePageOut(
        items=items,
        total=total,
        page=page,
        size=page_size,
        pages=(total + page_size - 1) // page_size,
        summary=UsageSummaryOut(**summary),
    )


@router.get("/roles", response_model=list[RoleOut], summary="三角色能力说明（只读）")
def list_roles() -> list[RoleOut]:
    """静态数据，不查库。

    文案写在 `schema.ROLE_CATALOG`：角色是 `users.role` 的 ENUM，不是一张表，
    多建一张表只会多一处需要同步的地方。
    """
    return [RoleOut(**item) for item in ROLE_CATALOG]