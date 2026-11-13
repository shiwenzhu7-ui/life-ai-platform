"""管理后台的查询层。

与 lifeweb 各模块的区别：这里的 `_base()` **不带 user_id 过滤**，
它本来就是给管理员看所有人的。权限由 router 层 `require_role("admin")` 把住，
不是靠这里加 where 条件 —— 这一点写进注释，免得后人照抄到别处。
"""

from datetime import datetime

from sqlalchemy import String, case, cast, func, or_, select
from sqlalchemy.orm import Session

from core.errors import not_found, validation_error
from modules.lifeweb.ai.models.usage_log import UsageLog
from modules.shared.users.model import User

# 一页最多 100 行，和 lifeweb 其他列表接口保持一致
_MAX_PAGE_SIZE = 100


def _limit_offset(page: int, size: int) -> tuple[int, int]:
    size = max(1, min(size, _MAX_PAGE_SIZE))
    page = max(1, page)
    return size, (page - 1) * size


def usage_counts(db: Session) -> dict[int, int]:
    """每个用户累计调用次数，一次查询拿全，避免列表里逐行 count（N+1）。"""
    rows = db.execute(
        select(UsageLog.user_id, func.count(UsageLog.id)).group_by(UsageLog.user_id)
    ).all()
    return {user_id: count for user_id, count in rows}


def list_users(
    db: Session,
    *,
    page: int = 1,
    size: int = 20,
    keyword: str | None = None,
    role: str | None = None,
    status: str | None = None,
) -> tuple[list[tuple[User, int]], int]:
    """用户列表。返回 ([(用户, 调用次数)], 总数)。

    软删用户（`deleted_at` 非空）**不显示** —— 管理员看不到"已注销"的人，
    要恢复得走别的手段。V1.4 没有注销功能，所以实际上等于全部显示。
    """
    size, offset = _limit_offset(page, size)

    base = select(User).where(User.deleted_at.is_(None))
    if role:
        base = base.where(User.role == role)
    if status:
        base = base.where(User.status == status)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        base = base.where(
            or_(
                User.username.like(like),
                User.nickname.like(like),
                User.email.like(like),
            )
        )

    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    rows = (
        db.execute(
            base.order_by(User.created_at.desc(), User.id.desc()).limit(size).offset(offset)
        )
        .scalars()
        .all()
    )

    counts = usage_counts(db)
    return [(row, counts.get(row.id, 0)) for row in rows], int(total)


def get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise not_found("用户不存在")
    return user


def update_user(
    db: Session, target: User, *, role: str | None, status: str | None, actor_id: int
) -> User:
    """改角色或状态。

    两个不能踩的坑，都在这里挡住：

    1. **管理员不能把自己降级或禁用**。他能点进去把自己改成 disabled，
       下次刷新就再也进不来，而库里又没有第二个管理员 —— 等于把自己锁在门外。
    2. **不能把最后一个管理员改掉**。哪怕改的是别人，如果库里只有他一个 admin，
       改完之后整个后台就没人能进了。
    """
    if role is None and status is None:
        raise validation_error("没有需要修改的字段")

    if target.id == actor_id:
        if role is not None and role != "admin":
            raise validation_error("不能修改自己的角色，请让其他管理员操作")
        if status is not None and status != "active":
            raise validation_error("不能禁用自己")

    if target.role == "admin" and (role not in (None, "admin") or status == "disabled"):
        admins = db.execute(
            select(func.count(User.id)).where(
                User.role == "admin", User.status == "active", User.deleted_at.is_(None)
            )
        ).scalar_one()
        if admins <= 1:
            raise validation_error("这是最后一个可用的管理员，不能降级或禁用")

    if role is not None:
        target.role = role
    if status is not None:
        target.status = status

    db.commit()
    db.refresh(target)
    return target


def list_usage(
    db: Session,
    *,
    page: int = 1,
    size: int = 20,
    user_id: int | None = None,
    keyword: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    status: str | None = None,
) -> tuple[list[tuple[UsageLog, str | None]], int, dict]:
    """AI 调用明细 + 合计。

    `keyword` 匹配用户名（也允许直接搜用户 ID）。
    `start` / `end` 是左闭右开，别写成 `<=` —— 否则 23:59:59 那一秒会算进两天。
    """
    size, offset = _limit_offset(page, size)

    base = select(UsageLog, User.username).join(
        User, User.id == UsageLog.user_id, isouter=True
    )
    if user_id is not None:
        base = base.where(UsageLog.user_id == user_id)
    if status:
        base = base.where(UsageLog.status == status)
    if start is not None:
        base = base.where(UsageLog.created_at >= start)
    if end is not None:
        base = base.where(UsageLog.created_at < end)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        base = base.where(
            or_(User.username.like(like), cast(UsageLog.user_id, String).like(like))
        )

    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    rows = db.execute(
        base.order_by(UsageLog.created_at.desc(), UsageLog.id.desc())
        .limit(size)
        .offset(offset)
    ).all()

    # 合计必须**单独查**，不能基于上面的 base：
    # base 里已经 join 了 users、选的是 (UsageLog, User.username) 两列，
    # 拿它 .subquery() 再 select_from 会让 SQLAlchemy 把 ai_usage_logs 再挂一次，
    # 变成每行乘一次用户表，合计直接翻几倍（实测 5 条记录算出 25）。
    summary_query = select(
        func.coalesce(func.sum(UsageLog.token_in), 0),
        func.coalesce(func.sum(UsageLog.token_out), 0),
        func.coalesce(func.sum(UsageLog.cost_estimate), 0),
        func.count(UsageLog.id),
        func.coalesce(func.sum(case((UsageLog.status == "failed", 1), else_=0)), 0),
    )
    if user_id is not None:
        summary_query = summary_query.where(UsageLog.user_id == user_id)
    if status:
        summary_query = summary_query.where(UsageLog.status == status)
    if start is not None:
        summary_query = summary_query.where(UsageLog.created_at >= start)
    if end is not None:
        summary_query = summary_query.where(UsageLog.created_at < end)
    if keyword and keyword.strip():
        # 关键字可能匹配用户名，这时必须回 users 表；用 IN 子查询而不是 join，
        # 避免同样的重复计数问题
        like = f"%{keyword.strip()}%"
        matched = select(User.id).where(User.username.like(like))
        summary_query = summary_query.where(
            or_(UsageLog.user_id.in_(matched), cast(UsageLog.user_id, String).like(like))
        )

    summary_row = db.execute(summary_query).one()

    summary = {
        "token_in": int(summary_row[0] or 0),
        "token_out": int(summary_row[1] or 0),
        "cost_estimate": float(summary_row[2] or 0),
        "calls": int(summary_row[3] or 0),
        "failed": int(summary_row[4] or 0),
    }
    return [(row[0], row[1]) for row in rows], int(total), summary