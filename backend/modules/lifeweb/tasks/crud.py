"""待办的数据访问层 —— 只写 SQL，不写业务规则。

两条铁律（每个查询都必须满足）：
  1. 必须带 user_id 过滤 —— 数据隔离的最后一道防线
  2. 必须排除软删除（deleted_at IS NULL）

写操作默认在本层完成 commit：现在都是单表写入，事务边界就在一条语句里。

例外是 `create(commit=False)`（A5 第 5 步加的）：AI 确认卡片时，
「写入这条待办」和「把 ai_pending_actions 置成 confirmed」必须在同一个事务里 ——
分两次提交的话，中途崩掉就会得到「待办建了、卡片还显示待确认」，
再点一次确认又是一条重复待办。这时提交权上提到调用方（ai/execute.py）。
"""

from datetime import date, datetime, timedelta
from typing import Sequence

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session

from core.time import beijing_day_start_utc, now_utc, to_utc_naive
from modules.lifeweb.tasks.model import Task
from modules.lifeweb.tasks.schema import TaskCreate, TaskUpdate

# search() 的硬上限：AI 查询一次最多这么多条。
# 结果要原样回灌进下一轮请求体，放开到几百条就是几千 token 的账单，
# 而且模型反而更容易挑错行。
MAX_SEARCH_LIMIT = 10


def _base_query(user_id: int) -> Select:
    """所有查询的公共起点：限本人 + 未删除。"""
    return select(Task).where(Task.user_id == user_id, Task.deleted_at.is_(None))


# 优先级没有天然大小：数据库里是枚举，按字母排会是 high < low < medium（毫无意义）。
# 用 CASE 折算：high=3 / medium=2 / low=1 —— 数值越大越急，
# 于是 order=desc 就是「高 → 低」，和前端下拉的文字一致。
# 不用 FIELD() 是为了不绑 MySQL 方言。
_PRIORITY_RANK = case(
    (Task.priority == "high", 3),
    (Task.priority == "medium", 2),
    else_=1,
)


def _order_by(sort: str, order: str) -> list:
    """把 (sort, order) 翻成 SQLAlchemy 的 order_by 子句列表。

    三条排序键都补一个 `Task.id.desc()` 兜底：数据库不保证相同排序键的返回顺序稳定，
    不补的话两条记录可能在翻页时重复出现或彻底消失。

    due_at 排序额外带 `Task.due_at.is_(None)` 打头：MySQL 没有 NULLS LAST，
    用「是否为空」当第一键把空值永远压到最后（升序降序都一样，因为它是布尔值）。
    """
    descending = order == "desc"

    if sort == "created_at":
        return [
            Task.created_at.desc() if descending else Task.created_at.asc(),
            Task.id.desc(),
        ]

    if sort == "priority":
        # 按优先级排时，第二键用 due_at（同优先级里先到期的在前），空值仍置后
        return [
            _PRIORITY_RANK.desc() if descending else _PRIORITY_RANK.asc(),
            Task.due_at.is_(None),
            Task.due_at.asc(),
            Task.id.desc(),
        ]

    # 默认：due_at
    return [
        Task.due_at.is_(None),
        Task.due_at.desc() if descending else Task.due_at.asc(),
        Task.id.desc(),
    ]



def list_for_user(
    db: Session,
    user_id: int,
    *,
    page: int = 1,
    size: int = 20,
    status: list[str] | None = None,
    priority: str | None = None,
    category_id: int | None = None,
    keyword: str | None = None,
    due_from: date | None = None,
    due_to: date | None = None,
    overdue: bool = False,
    sort: str = "due_at",
    order: str = "asc",
) -> tuple[list[Task], int]:
    """当前用户的待办列表（分页 + 筛选），返回 (当页数据, 总数)。

    筛选条件用 conds 列表累积，最后一次性 where(*conds) ——
    这样加条件不用改 SQL 结构，也不会漏掉某个分支。

    total 必须按「筛选后」的条件算，否则分页器显示「共 11 条」但实际只筛出 3 条。

    排序默认按 docs/接口设计.md §1.5：due_at 升序、空值置后；
    同一时刻的再用 id 倒序兜底 —— 否则两条 due_at 相同的记录在翻页时
    可能第 1 页和第 2 页都出现（数据库不保证相同排序键的返回顺序稳定）。
    具体排序键由 `_order_by(sort, order)` 决定，默认参数保持原行为。
    """
    conds: list = []

    if status:
        conds.append(Task.status.in_(status))

    if priority:
        conds.append(Task.priority == priority)

    # 注意不能写 if category_id: —— 传 0 是「只看未分类」，是个有效筛选
    if category_id is not None:
        conds.append(Task.category_id == category_id)

    if keyword and keyword.strip():
        # 用户输入永远不能直接拼进 LIKE：% 和 _ 是通配符。
        # 顺序不能反 —— 必须先把 \ 换成 \\，否则后面加进去的转义符会被再转义一次。
        like = _like_parts(keyword)
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

    # 排序见 _order_by：默认「到期升序，空值置后，id 倒序兜底」
    stmt = base.order_by(*_order_by(sort, order)).offset((page - 1) * size).limit(size)
    items = list(db.scalars(stmt).all())
    return items, int(total)


def get_for_user(db: Session, user_id: int, task_id: int) -> Task | None:
    """按 ID 取一条。

    不属于该用户时返回 None，对外统一表现为 404 ——
    403 等于告诉对方「这条存在，只是你没权限」，可以被用来探测别人的数据量。
    """
    return db.scalar(_base_query(user_id).where(Task.id == task_id))


def _like_parts(keyword: str) -> str:
    """把用户输入包成 LIKE 模式。

    用户输入永远不能直接拼进 LIKE：% 和 _ 是通配符，一个「100%」能把整张表捞出来。
    顺序不能反 —— 必须先把 \\ 换成 \\\\，否则后面加进去的转义符会被再转义一次。
    """
    raw = keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{raw}%"


def search(
    db: Session,
    user_id: int,
    *,
    keyword: str | None = None,
    statuses: Sequence[str] | None = None,
    category_id: int | None = None,
    due_before: datetime | None = None,
    limit: int = 5,
) -> list[Task]:
    """给 AI 查询工具用的精简查询：限本人 + 未删除，按「快到期的在前」取前 N 条。

    与 `list_for_user` 的区别（**不是重复实现**）：
      - 没有分页、不数 total：模型只要一份清单，不需要「共 37 条」
      - `statuses` 是列表：查询 todo 时要同时带上 doing（进行中的也还没做完）
      - `due_before` 收 datetime（A6 的调用方已带时区），这里转 naive UTC 再比较
      - 排序固定：先按 due_at 升序、空值最后、id 倒序兜底
        —— 模型拿到的顺序就是它该照着说的顺序，不该由用户翻页决定

    `limit` 在这里再夹一次（1..MAX_SEARCH_LIMIT）：上层已经夹过，
    但这是最后一道，写死一个数比信任调用方便宜。
    """
    conds: list = []

    if statuses:
        conds.append(Task.status.in_(list(statuses)))

    if category_id is not None:
        conds.append(Task.category_id == category_id)

    if keyword and keyword.strip():
        # 只搜标题：AI 查询是为了「认出是哪一条」，备注太长、
        # 而且按备注命中反而会让清单里出现标题毫不相关的条目
        conds.append(Task.title.like(_like_parts(keyword), escape="\\"))

    if due_before is not None:
        conds.append(Task.due_at.is_not(None))
        conds.append(Task.due_at < to_utc_naive(due_before))

    base = _base_query(user_id)
    if conds:
        base = base.where(*conds)

    size = min(max(1, int(limit)), MAX_SEARCH_LIMIT)
    stmt = base.order_by(
        Task.due_at.is_(None), Task.due_at.asc(), Task.id.desc()
    ).limit(size)
    return list(db.scalars(stmt).all())


def create(
    db: Session, user_id: int, payload: TaskCreate, *, commit: bool = True
) -> Task:
    """新建待办。

    四个要点：
      1. user_id / status 由服务端决定，不读请求体
      2. due_at 入库前必须转成 naive UTC（库里只存 UTC）
      3. commit 之后的 refresh 不能省：created_at 是数据库默认值，
         不 refresh 拿回来就是 None，响应里会缺字段
      4. `commit=False` 时只 flush 拿到自增 id，提交权交给调用方
         （AI 确认卡片要和 pending 状态变更同事务，见模块 docstring）
    """
    task = Task(
        user_id=user_id,
        title=payload.title,
        description=payload.description,
        category_id=payload.category_id,
        priority=payload.priority,
        status="todo",
        due_at=to_utc_naive(payload.due_at),
    )
    db.add(task)
    if not commit:
        db.flush()  # 只为了拿到 task.id；没提交，外面回滚它就不存在
        return task
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
