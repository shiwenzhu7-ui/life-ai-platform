"""待办业务逻辑层 —— 这一层由你实现。

调用链：路由层（plumbing，已写好）→ **service（本层，业务规则）** → repository（已写好）→ 数据库

约定：
1. 本层负责业务规则，并负责**事务提交**（db.commit）
2. user_id 永远由路由层从依赖注入传入，绝不允许从请求体读取
3. 查不到资源一律抛 `not_found()`（404），**不要抛 403**——避免泄露资源是否存在
4. 进入本层时时间为 naive UTC；出参由 TaskOut 统一转成 +08:00

已实现两个示例：`list_tasks` / `get_task`。照它们的风格实现剩下 4 个。
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import not_found
from app.core.time import now_utc, to_utc_naive
from app.models.task import Task
from app.repositories.task_repo import TaskRepository
from app.schemas.task import TaskCreate, TaskUpdate


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
    """TODO 待你实现：新建待办。

    需要做的事：
      1. 校验 `payload.category_id`：必须属于 `scope="task"`，且是系统预置或本人自建。
         可用 `CategoryRepository(db).exists_in_scope(category_id, "task", user_id)`，
         不合法时抛 `validation_error("分类不存在")`（from app.core.errors import validation_error）
      2. 时间字段 `due_at` / `remind_at` 用 `to_utc_naive()` 转成 naive UTC 再存
      3. 组装 `Task`：`user_id` 用参数传进来的值，`source="manual"`，`status="todo"`
      4. `TaskRepository(db).add(task)` → `db.commit()` → `db.refresh(task)`
      5. 返回 task

    验收标准（tests/test_tasks.py 中的用例）：
      - test_create_task_ok           201，title/category_name 正确，due_at 带 +08:00
      - test_create_task_blank_title  422（Schema 已拦截，确认错误体能透出）
      - test_create_task_bad_category 422，message 为「分类不存在」
      - test_create_task_remind_after_due 422，message 含「提醒时间必须早于截止时间」
    """
    raise NotImplementedError("create_task 待你实现（见函数 docstring）")


def update_task(db: Session, user_id: int, task_id: int, payload: TaskUpdate) -> Task:
    """TODO 待你实现：局部更新。

    需要做的事：
      1. 先 `get_task()` 拿到对象（不存在抛 404）
      2. **只更新显式传了的字段**：用 `payload.model_dump(exclude_unset=True)`
         注意区分「没传这个键」和「传了 null」——后者表示清空（例如 due_at=null）
      3. 时间字段转 naive UTC；若改了 category_id，同样要校验归属
      4. `db.commit()` + `db.refresh(task)`

    验收标准：
      - test_update_partial        只改 title，description/priority 保持不变
      - test_update_clear_due      显式传 due_at=null 后，响应里 due_at 为 null
      - test_update_not_found      改不存在的 ID → 404
    """
    raise NotImplementedError("update_task 待你实现（见函数 docstring）")


def change_status(db: Session, user_id: int, task_id: int, status: str) -> Task:
    """TODO 待你实现：状态切换 —— 本模块最值得动手写的一个。

    业务规则：
      1. 目标状态为 `"done"` 时，把 `completed_at` 设为 `now_utc()`
      2. 目标状态不是 `"done"` 时（退回 todo/doing，或标为 cancelled），
         把 `completed_at` 清空为 `None`
      3. 幂等：已经是 done 的任务再次设为 done，`completed_at` **保持第一次的时间不变**
         （提示：先判断 `task.status == "done"`，再决定是否写时间）
      4. 状态值合法性由 Schema 的 Literal 保证，本层可不再重复校验

    验收标准：
      - test_status_to_done_sets_completed_at   completed_at 不为 null
      - test_status_reopen_clears_completed_at  改回 todo 后 completed_at 为 null
      - test_status_idempotent                  连续两次 done，completed_at 不变
    """
    raise NotImplementedError("change_status 待你实现（见函数 docstring）")


def delete_task(db: Session, user_id: int, task_id: int) -> None:
    """TODO 待你实现：软删除。

    需要做的事：
      1. 先 `get_task()`（不存在抛 404）
      2. `TaskRepository(db).soft_delete(task)` —— **只打 deleted_at，不物理删除**
      3. `db.commit()`

    验收标准：
      - test_delete_then_get_404   删除后 GET 返回 404
      - test_delete_twice_404      第二次删除也是 404（不能变成 500）
    """
    raise NotImplementedError("delete_task 待你实现（见函数 docstring）")
