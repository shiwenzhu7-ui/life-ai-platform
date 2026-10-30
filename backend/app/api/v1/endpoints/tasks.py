"""待办事项接口（plumbing 层，已写好）。

这一层只做三件事，不含业务规则：
  1. 绑定并校验 HTTP 参数（Query / Body）
  2. 注入当前 user_id（V1.0 固定演示用户，V1.2 换成 JWT 解析，本层不用改）
  3. 调 service，把 ORM 对象转成响应 Schema
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.core.deps import get_current_user_id
from app.db.session import get_db
from app.repositories.category_repo import CategoryRepository
from app.schemas.common import Page
from app.schemas.task import TaskCreate, TaskOut, TaskStatusUpdate, TaskUpdate
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["待办事项"])

CATEGORY_SCOPE = "task"


def _to_out(db: Session, user_id: int, task) -> TaskOut:
    """单个对象转响应体，顺带补上分类名称。"""
    name = CategoryRepository(db).name_map(CATEGORY_SCOPE, user_id).get(task.category_id)
    return TaskOut.from_model(task, name)


@router.get("", response_model=Page[TaskOut], summary="待办列表")
def list_tasks(
    status: list[str] | None = Query(default=None, description="状态多选，如 ?status=todo&status=doing"),
    category_id: int | None = Query(default=None, ge=0),
    priority: str | None = Query(default=None, pattern="^(low|medium|high)$"),
    due_from: datetime | None = Query(default=None, description="截止时间起（含）"),
    due_to: datetime | None = Query(default=None, description="截止时间止（含）"),
    keyword: str | None = Query(default=None, max_length=50),
    overdue: bool = Query(default=False, description="只看逾期"),
    sort: str = Query(default="due_at", description="排序字段，前缀 - 表示降序"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Page[TaskOut]:
    items, total = task_service.list_tasks(
        db,
        user_id,
        statuses=status,
        category_id=category_id,
        priority=priority,
        due_from=due_from,
        due_to=due_to,
        keyword=keyword,
        only_overdue=overdue,
        sort=sort,
        page=page,
        size=size,
    )
    names = CategoryRepository(db).name_map(CATEGORY_SCOPE, user_id)
    return Page.create(
        [TaskOut.from_model(t, names.get(t.category_id)) for t in items],
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
    task = task_service.create_task(db, user_id, payload)
    return _to_out(db, user_id, task)


@router.get("/{task_id}", response_model=TaskOut, summary="待办详情")
def get_task(
    task_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> TaskOut:
    task = task_service.get_task(db, user_id, task_id)
    return _to_out(db, user_id, task)


@router.patch("/{task_id}", response_model=TaskOut, summary="修改待办")
def update_task(
    task_id: int,
    payload: TaskUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> TaskOut:
    task = task_service.update_task(db, user_id, task_id, payload)
    return _to_out(db, user_id, task)


@router.patch("/{task_id}/status", response_model=TaskOut, summary="切换状态")
def change_status(
    task_id: int,
    payload: TaskStatusUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> TaskOut:
    task = task_service.change_status(db, user_id, task_id, payload.status)
    return _to_out(db, user_id, task)


@router.delete("/{task_id}", status_code=204, summary="删除待办（软删除）")
def delete_task(
    task_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Response:
    task_service.delete_task(db, user_id, task_id)
    return Response(status_code=204)
