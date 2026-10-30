"""待办事项数据访问层。

安全约定（见 docs/数据库设计.md §6）：
- **每一个方法都必须带 user_id 过滤**，这是数据隔离的最后一道防线
- 查询默认排除软删除记录（deleted_at IS NULL）
- 事务提交由 service 层负责，本层只做 add / flush，便于组合多个写操作
"""

from datetime import datetime

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.time import now_utc
from app.models.task import Task

# 允许排序的字段白名单（防止把任意字段名拼进 SQL）
SORTABLE: dict[str, object] = {
    "id": Task.id,
    "due_at": Task.due_at,
    "created_at": Task.created_at,
    "priority": Task.priority,
    "status": Task.status,
    "title": Task.title,
}


class TaskRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------- 查询 ----------

    def _base_query(self, user_id: int) -> Select:
        return select(Task).where(Task.user_id == user_id, Task.deleted_at.is_(None))

    def list_for_user(
        self,
        user_id: int,
        *,
        statuses: list[str] | None = None,
        category_id: int | None = None,
        priority: str | None = None,
        due_from: datetime | None = None,
        due_to: datetime | None = None,
        keyword: str | None = None,
        only_overdue: bool = False,
        sort: str = "due_at",
        page: int = 1,
        size: int = 20,
        now: datetime | None = None,
    ) -> tuple[list[Task], int]:
        """按条件分页查询，返回 (当页数据, 总数)。"""
        stmt = self._base_query(user_id)

        if statuses:
            stmt = stmt.where(Task.status.in_(statuses))
        if category_id is not None:
            stmt = stmt.where(Task.category_id == category_id)
        if priority:
            stmt = stmt.where(Task.priority == priority)
        if due_from is not None:
            stmt = stmt.where(Task.due_at.is_not(None), Task.due_at >= due_from)
        if due_to is not None:
            stmt = stmt.where(Task.due_at.is_not(None), Task.due_at <= due_to)
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(or_(Task.title.like(like), Task.description.like(like)))
        if only_overdue:
            reference = now or now_utc()
            stmt = stmt.where(
                Task.due_at.is_not(None),
                Task.due_at < reference,
                Task.status.in_(("todo", "doing")),
            )

        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = self._apply_sort(stmt, sort).offset((page - 1) * size).limit(size)
        items = list(self.db.scalars(stmt).all())
        return items, int(total)

    def _apply_sort(self, stmt: Select, sort: str) -> Select:
        """排序：due_at 默认升序且空值排最后；支持 -字段 表示降序。"""
        desc = sort.startswith("-")
        field = sort.lstrip("-") or "due_at"

        if field == "due_at" or field not in SORTABLE:
            order = Task.due_at.desc() if desc else Task.due_at.asc()
            # MySQL 里 `due_at IS NULL` 结果为 0/1，升序即「有值在前」
            return stmt.order_by(Task.due_at.is_(None), order, Task.id.desc())

        column = SORTABLE[field]
        return stmt.order_by(column.desc() if desc else column.asc(), Task.id.desc())

    def get_for_user(self, user_id: int, task_id: int) -> Task | None:
        """按 ID 取单条；不属于该用户时返回 None（对外表现为 404，避免泄露存在性）。"""
        stmt = self._base_query(user_id).where(Task.id == task_id)
        return self.db.scalar(stmt)

    def count_by_status(self, user_id: int) -> dict[str, int]:
        """各状态数量，用于首页仪表盘。"""
        stmt = (
            select(Task.status, func.count())
            .where(Task.user_id == user_id, Task.deleted_at.is_(None))
            .group_by(Task.status)
        )
        counts = {status: 0 for status in ("todo", "doing", "done", "cancelled")}
        for status, count in self.db.execute(stmt).all():
            counts[status] = int(count)
        return counts

    def count_overdue(self, user_id: int, now: datetime | None = None) -> int:
        reference = now or now_utc()
        stmt = select(func.count()).where(
            Task.user_id == user_id,
            Task.deleted_at.is_(None),
            Task.due_at.is_not(None),
            Task.due_at < reference,
            Task.status.in_(("todo", "doing")),
        )
        return int(self.db.scalar(stmt) or 0)

    def count_due_between(self, user_id: int, start: datetime, end: datetime) -> int:
        stmt = select(func.count()).where(
            Task.user_id == user_id,
            Task.deleted_at.is_(None),
            Task.due_at.is_not(None),
            Task.due_at >= start,
            Task.due_at < end,
        )
        return int(self.db.scalar(stmt) or 0)

    def list_upcoming(self, user_id: int, limit: int = 5, now: datetime | None = None) -> list[Task]:
        """最近待办（未完成、有截止时间），用于首页。"""
        reference = now or now_utc()
        stmt = (
            self._base_query(user_id)
            .where(Task.status.in_(("todo", "doing")), Task.due_at.is_not(None))
            .order_by(Task.due_at.asc())
            .limit(limit)
        )
        del reference  # 保留参数以便未来按 now 过滤
        return list(self.db.scalars(stmt).all())

    # ---------- 写入（不提交事务，由 service 决定） ----------

    def add(self, task: Task) -> Task:
        self.db.add(task)
        self.db.flush()
        return task

    def soft_delete(self, task: Task, when: datetime | None = None) -> None:
        task.deleted_at = when or now_utc()
        self.db.flush()
