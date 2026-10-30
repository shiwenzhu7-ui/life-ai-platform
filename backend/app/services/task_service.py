"""待办业务逻辑层。

调用链：路由层（plumbing，已写好）→ **service（本层，业务规则）** → repository（已写好）→ 数据库

约定：
1. 本层负责业务规则，并负责**事务提交**（db.commit）
2. user_id 永远由路由层从依赖注入传入，绝不允许从请求体读取
3. 查不到资源一律抛 `not_found()`（404），**不要抛 403**——避免泄露资源是否存在
4. 进入本层时时间为 naive UTC；出参由 TaskOut 统一转成 +08:00
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import not_found, validation_error
from app.core.time import now_utc, to_utc_naive
from app.models.task import Task
from app.repositories.category_repo import CategoryRepository
from app.repositories.task_repo import TaskRepository
from app.schemas.task import TaskCreate, TaskUpdate

CATEGORY_SCOPE = "task"

# 这些字段数据库不允许为 NULL：客户端传 null 时按「不改」处理，而不是写坏数据
NOT_NULLABLE_FIELDS = ("title", "category_id", "priority")


def _ensure_category(db: Session, user_id: int, category_id: int) -> None:
    """分类必须属于 task 用途域，且是系统预置或本人自建；0 表示未分类，合法。"""
    if not CategoryRepository(db).exists_in_scope(category_id, CATEGORY_SCOPE, user_id):
        raise validation_error("分类不存在")


def list_tasks(db: Session, user_id: int, **filters) -> tuple[list[Task], int]:
    """【示例】待办列表。

    筛选、排序、分页的逻辑都在 repository 里，service 只做两件事：
    把时间参数统一成 naive UTC、把请求交给 repository。
    """
    repo = TaskRepository(db)
    now = now_utc()
    for key in ("due_from", "due_to"):
        value = filters.get(key)
        if isinstance(value, datetime):
            filters[key] = to_utc_naive(value)
    return repo.list_for_user(user_id, now=now, **filters)


def get_task(db: Session, user_id: int, task_id: int) -> Task:
    """【示例】取单条待办。

    查不到、或不属于当前用户，都返回同一个 404 —— 外部无法区分
    「不存在」和「存在但不是你的」。
    """
    repo = TaskRepository(db)
    task = repo.get_for_user(user_id, task_id)
    if task is None:
        raise not_found("待办不存在")
    return task


def create_task(db: Session, user_id: int, payload: TaskCreate) -> Task:
    """新建待办。

    顺序：校验分类 → 转时间 → 组装对象 → 落库提交 → 回读。
    """
    _ensure_category(db, user_id, payload.category_id)

    due_at = to_utc_naive(payload.due_at) if payload.due_at else None
    remind_at = to_utc_naive(payload.remind_at) if payload.remind_at else None
    # Schema 已在入参层拦过一遍；这里用统一时区再判一次，防止两个字段一个带偏移一个不带
    if due_at is not None and remind_at is not None and remind_at >= due_at:
        raise validation_error("提醒时间必须早于截止时间")

    task = Task(
        user_id=user_id,  # 只能来自依赖注入，绝不读请求体
        title=payload.title,
        description=payload.description,
        category_id=payload.category_id,
        priority=payload.priority,
        status="todo",
        source="manual",
        due_at=due_at,
        remind_at=remind_at,
    )
    TaskRepository(db).add(task)
    db.commit()
    db.refresh(task)  # 取回 created_at / updated_at 等数据库生成的默认值
    return task


def update_task(db: Session, user_id: int, task_id: int, payload: TaskUpdate) -> Task:
    """局部更新。

    关键点：`exclude_unset=True` 才能区分「没传这个键」（不改）和「传了 null」（清空）。
    """
    task = get_task(db, user_id, task_id)
    data = payload.model_dump(exclude_unset=True)

    if data.get("category_id") is not None:
        _ensure_category(db, user_id, data["category_id"])

    for key in ("due_at", "remind_at"):
        if data.get(key) is not None:
            data[key] = to_utc_naive(data[key])

    # 只改了一个时间字段时，另一个要用库里的现值一起判断先后
    due_at = data["due_at"] if "due_at" in data else task.due_at
    remind_at = data["remind_at"] if "remind_at" in data else task.remind_at
    if due_at is not None and remind_at is not None and remind_at >= due_at:
        raise validation_error("提醒时间必须早于截止时间")

    for key, value in data.items():
        if value is None and key in NOT_NULLABLE_FIELDS:
            continue  # 这些列不允许 NULL，null 视为「不改」
        setattr(task, key, value)

    db.commit()
    db.refresh(task)
    return task


def change_status(db: Session, user_id: int, task_id: int, status: str) -> Task:
    """状态切换。

    业务规则：进入 done 时记录完成时间；离开 done 时清空完成时间；
    重复设为 done 不刷新完成时间（幂等）。
    """
    task = get_task(db, user_id, task_id)

    if status == "done":
        # 已经是 done 就不动 completed_at，保留第一次完成的时间
        if task.status != "done" or task.completed_at is None:
            task.completed_at = now_utc()
    else:
        task.completed_at = None

    task.status = status
    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, user_id: int, task_id: int) -> None:
    """软删除：只打 deleted_at，不物理删除。

    已删除的记录在 repository 的 `deleted_at IS NULL` 条件下查不到，
    所以第二次删除自然落到 404，不会变成 500。
    """
    task = get_task(db, user_id, task_id)
    TaskRepository(db).soft_delete(task)
    db.commit()
