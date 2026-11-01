"""待办接口 —— 只做三件事，不含业务规则。

  1. 绑定并校验 HTTP 参数（Query / Body）
  2. 注入 user_id（V1.0 固定演示用户；V1.2 换成 JWT 解析，本层不用改）
  3. 调 crud，把 ORM 对象转成响应 Schema
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import get_current_user_id
from core.errors import not_found, validation_error
from core.time import now_utc
from modules.shared.pagination import Page
from modules.lifeweb.tasks import crud
from modules.lifeweb.tasks.schema import (
    Priority,
    TaskCreate,
    TaskOut,
    TaskStatus,
    TaskStatusUpdate,
    TaskUpdate,
)

router = APIRouter(prefix="/tasks", tags=["待办"])


def _get_or_404(db: Session, user_id: int, task_id: int):
    """取一条，取不到就 404。

    详情 / 修改 / 切状态 / 删除 四个接口都要做同一个判断，
    写成一处，以后改「查不到怎么报错」只需要改这里。
    """
    task = crud.get_for_user(db, user_id, task_id)
    if task is None:
        raise not_found("待办不存在")
    return task


@router.get("", response_model=Page[TaskOut], summary="待办列表")
def list_tasks(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    status: list[TaskStatus] | None = Query(
        default=None, description="多值：?status=todo&status=doing"
    ),
    priority: Priority | None = Query(default=None, description="low / medium / high"),
    keyword: str | None = Query(
        default=None, max_length=50, description="模糊匹配标题或备注"
    ),
    due_from: date | None = Query(
        default=None, description="截止日期起（含当天），YYYY-MM-DD"
    ),
    due_to: date | None = Query(
        default=None, description="截止日期止（含当天），YYYY-MM-DD"
    ),
    overdue: bool = Query(default=False, description="true 只看逾期"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Page[TaskOut]:
    """列表 + 筛选。

    未声明的查询参数会被 FastAPI 静默忽略（照样返回 200，只是没筛），
    所以文档里写了的每个筛选参数都必须在这里显式声明。
    """
    if due_from and due_to and due_from > due_to:
        raise validation_error("due_from 不能晚于 due_to")

    items, total = crud.list_for_user(
        db,
        user_id,
        page=page,
        size=size,
        status=status,
        priority=priority,
        keyword=keyword,
        due_from=due_from,
        due_to=due_to,
        overdue=overdue,
    )
    now = now_utc()  # 同一个请求里只取一次时间，保证整页的 is_overdue 口径一致
    return Page.create(
        [TaskOut.from_model(task, now) for task in items],
        total,
        page,
        size,
    )


@router.post("", response_model=TaskOut, status_code=201, summary="新建待办")
def create_task(
    payload: TaskCreate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> TaskOut:
    """新建。

    能走到这里，说明请求体已经过 Pydantic 校验
    （空标题、非法 priority 会在进入本函数之前返回 422）。
    """
    task = crud.create(db, user_id, payload)
    return TaskOut.from_model(task, now_utc())


@router.get("/{task_id}", response_model=TaskOut, summary="待办详情")
def get_task(
    task_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> TaskOut:
    """取一条详情。

    三种查不到的情况（ID 不存在 / 已软删除 / 是别人的）一律返回 404，
    不区分 —— 返回 403 等于告诉对方「这条确实存在，只是不给你看」。
    """
    return TaskOut.from_model(_get_or_404(db, user_id, task_id), now_utc())


@router.patch("/{task_id}", response_model=TaskOut, summary="修改待办")
def update_task(
    task_id: int,
    payload: TaskUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> TaskOut:
    """局部更新。

    能走到这里，说明请求体已经过校验：
      空对象 {}          → 422「没有需要修改的字段」
      title / priority 传 null → 422（数据库里它们是 NOT NULL）
      空白标题           → 422「标题不能为空」
    真正「哪些字段要改」的判断在 crud.update 里，靠 exclude_unset 完成。
    """
    task = _get_or_404(db, user_id, task_id)
    task = crud.update(db, task, payload)
    return TaskOut.from_model(task, now_utc())


@router.patch("/{task_id}/status", response_model=TaskOut, summary="切换待办状态")
def change_task_status(
    task_id: int,
    payload: TaskStatusUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> TaskOut:
    """勾选 / 取消勾选 / 改成进行中。

    与 PATCH /{task_id} 分开的原因：
      1. 前端在这里只发 {"status": "done"}，不需要把整个对象传来
      2. 这条路径要联动 completed_at，规则比「改文案」重
      3. 以后 V1.1 要加「完成任务 → 加积分 / 发事件」的副作用，加在这里不影响普通编辑

    status 的合法性由 Literal 校验（写 "finished" 会 422），
    completed_at 的维护在 crud.change_status 里（幂等 + 清空两条规则）。
    """
    task = _get_or_404(db, user_id, task_id)
    task = crud.change_status(db, task, payload.status)
    return TaskOut.from_model(task, now_utc())


@router.delete("/{task_id}", status_code=204, summary="删除待办")
def delete_task(
    task_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> None:
    """软删除（只写 deleted_at，不真删）。

    三条行为都需要保证：
      1. 删完再查详情 → 404（_base_query 排除了 deleted_at 有值的行）
      2. 连删两次 → 第二次也是 404，不是 500（第二次根本查不到这条）
      3. 删别人的 → 404，不泄露「这条存在」

    返回 204 无响应体：前端拿到后直接把那一行从列表里移除，不需要新数据。
    """
    crud.soft_delete(db, _get_or_404(db, user_id, task_id))
