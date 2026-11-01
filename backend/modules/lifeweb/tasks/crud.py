"""待办的数据访问层 —— 只写 SQL，不写业务规则。

两条铁律（每个查询都必须满足）：
  1. 必须带 user_id 过滤 —— 数据隔离的最后一道防线
  2. 必须排除软删除（deleted_at IS NULL）

写操作在本层完成 commit：现在都是单表写入，事务边界就在一条语句里。
等 V1.1 出现「一次操作改多张表」的需求，再把它上提到 service 层，
否则每多一层就多一次「谁负责提交」的含糊地带。
"""

from datetime import date, timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from core.time import beijing_day_start_utc, now_utc, to_utc_naive
from modules.lifeweb.tasks.model import Task
from modules.lifeweb.tasks.schema import TaskCreate, TaskUpdate


def _base_query(user_id: int) -> Select:
    """所有查询的公共起点：限本人 + 未删除。"""
    return select(Task).where(Task.user_id == user_id, Task.deleted_at.is_(None))


def list_for_user(
    db: Session,
    user_id: int,
    *,
    page: int = 1,
    size: int = 20,
    status: list[str] | None = None,
    priority: str | None = None,
    keyword: str | None = None,
    due_from: date | None = None,
    due_to: date | None = None,
    overdue: bool = False,
) -> tuple[list[Task], int]:
    """当前用户的待办列表（分页 + 筛选），返回 (当页数据, 总数)。

    筛选条件用 conds 列表累积，最后一次性 where(*conds) ——
    这样加条件不用改 SQL 结构，也不会漏掉某个分支。

    total 必须按「筛选后」的条件算，否则分页器显示「共 11 条」但实际只筛出 3 条。

    排序按 docs/接口设计.md §1.5：due_at 升序、空值置后；
    同一时刻的再用 id 倒序兜底 —— 否则两条 due_at 相同的记录在翻页时
    可能第 1 页和第 2 页都出现（数据库不保证相同排序键的返回顺序稳定）。
    """
    conds: list = []

    if status:
        conds.append(Task.status.in_(status))

    if priority:
        conds.append(Task.priority == priority)

    if keyword and keyword.strip():
        # 用户输入永远不能直接拼进 LIKE：% 和 _ 是通配符。
        # 顺序不能反 —— 必须先把 \ 换成 \\，否则后面加进去的转义符会被再转义一次。
        raw = keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{raw}%"
        # 关键字匹配标题或备注（docs/接口设计.md §1.3）
        conds.append(
            or_(
                Task.title.like(like, escape="\\"),
                Task.description.like(like, escape="\\"),
            )
        )

    if due_from:
        conds.append(Task.due_at >= beijing_day_start_utc(due_from))

    if due_to:
        # 半开区间：包含当天 = < 次日 00:00（北京时间）
        conds.append(Task.due_at < beijing_day_start_utc(due_to + timedelta(days=1)))

    if overdue:
        # 口径见 docs/数据库设计.md：due_at < now AND status IN ('todo','doing')
        conds.append(Task.due_at.is_not(None))
        conds.append(Task.due_at < now_utc())
        conds.append(Task.status.in_(("todo", "doing")))

    base = _base_query(user_id)
    if conds:
        base = base.where(*conds)

    # 总数要在分页之前算，否则拿到的是「当页条数」
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    # MySQL 没有 NULLS LAST：先按「是否 NULL」升序（NULL 排最后），再按 due_at 升序
    stmt = (
        base.order_by(Task.due_at.is_(None), Task.due_at.asc(), Task.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    items = list(db.scalars(stmt).all())
    return items, int(total)


def get_for_user(db: Session, user_id: int, task_id: int) -> Task | None:
    """按 ID 取一条。

    不属于该用户时返回 None，对外统一表现为 404 ——
    403 等于告诉对方「这条存在，只是你没权限」，可以被用来探测别人的数据量。
    """
    return db.scalar(_base_query(user_id).where(Task.id == task_id))


def create(db: Session, user_id: int, payload: TaskCreate) -> Task:
    """新建待办。

    三个要点：
      1. user_id / status 由服务端决定，不读请求体
      2. due_at 入库前必须转成 naive UTC（库里只存 UTC）
      3. commit 之后的 refresh 不能省：created_at 是数据库默认值，
         不 refresh 拿回来就是 None，响应里会缺字段
    """
    task = Task(
        user_id=user_id,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        status="todo",
        due_at=to_utc_naive(payload.due_at),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def update(db: Session, task: Task, payload: TaskUpdate) -> Task:
    """局部更新一条。

    核心是 `exclude_unset=True`：只吐出「请求体里真的出现过」的字段。
      {"title": "x"}                → {"title": "x"}         只改标题
      {"description": null}         → {"description": None}  清空描述
      {}                            → {}                     一个都不改

    注意不能直接用 payload.__dict__ 或 model_dump()：
    那会把没传的字段也当成 None 写回去，一次改标题就把描述清空了。
    """
    changes = payload.model_dump(exclude_unset=True)

    for key, value in changes.items():
        if key == "due_at":
            value = to_utc_naive(value)  # 入库前统一转 naive UTC
        setattr(task, key, value)

    db.commit()
    db.refresh(task)  # due_at 等列的类型由数据库确认后再返回给前端
    return task


def change_status(db: Session, task: Task, status: str) -> Task:
    """切状态，并维护 completed_at —— 两条规则缺一不可。

    规则 1（幂等）：已经是 done 再切一次 done，completed_at 不许刷新。
        否则「完成时间」会变成「最后一次点它的时间」，
        统计「本周完成了几条 / 平均耗时」全都会漂。

    规则 2（清空）：任何非 done 状态，completed_at 一律置 None。
        否则会出现「未完成但有完成时间」的自相矛盾数据，
        看板统计完成数时会虚高。

    注意判断顺序：先读 task.status（旧值）再赋值，
    写成 task.status = status 之后再去判断，规则 1 就失效了。
    """
    if status == "done":
        if task.status != "done" or task.completed_at is None:
            task.completed_at = now_utc()
    else:
        task.completed_at = None

    task.status = status
    db.commit()
    db.refresh(task)
    return task


def soft_delete(db: Session, task: Task) -> None:
    """软删除 —— 只写 deleted_at，不执行 DELETE 语句。

    为什么不真删（三条理由都跟「以后还要查」有关）：
      1. 真删之后「本月删了几条」这类统计就没有数据可算
      2. 记录无痕消失，出了问题没法复现
      3. 误删无法恢复

    不返回 Task：接口回 204，没有响应体。
    调用方（router）已经用它查过一次，那条记录在本次事务里是活的。
    """
    task.deleted_at = now_utc()
    db.commit()
