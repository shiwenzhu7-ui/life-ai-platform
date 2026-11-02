"""首页的取数层 —— 跨模块只读聚合。

与其它模块的 crud.py 有三点不同，值得先说清：

1. 名字叫 queries 而不是 crud。crud 的约定是「只碰自己模块的表」；
   这里必须同时读 tasks 与 expenses，叫 crud 会让那个约定慢慢失效。
2. 只有 SELECT，没有任何写操作 —— 打首页永远不改变数据。
3. 不能复用 tasks/expenses 的 `_base_query`：那个返回的是**实体查询**，
   这里要的是聚合列。所以自己定义了 `_task_scope` / `_expense_scope`，
   所有 SQL 都从它们出发 —— 代价是过滤条件要再写一遍，
   收益是「忘了 user_id 或 deleted_at」这件事只可能在这两个函数里发生。

时间口径（全部按北京时间理解、用半开区间）：
  今天   [今天 00:00, 明天 00:00)
  本月   [本月 1 日 00:00, 下月 1 日 00:00)      上月 = shift_month(-1) 之后再取
  近 7 天 今天往前 6 天到明天 00:00（含今天，共 7 天）
  逾期   due_at < 现在 且 status in (todo, doing)
  今日到期 due_at 落在今天 且 status in (todo, doing)
  逾期与今日到期 due_at < 明天 00:00 且 status in (todo, doing)，按 due_at 升序取前 5
"""

from datetime import date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from core.time import (
    beijing_day_start_utc,
    beijing_month_range,
    now_beijing,
    shift_month,
    to_beijing_iso,
    to_utc_naive,
)
from modules.lifeweb.dashboard.schema import (
    DashboardOverview,
    ExpenseStats,
    FocusTask,
    TaskStats,
)
from modules.lifeweb.expenses.crud import daily_totals
from modules.lifeweb.expenses.model import Expense
from modules.lifeweb.expenses.schema import DayTotal
from modules.lifeweb.tasks.model import Task
from modules.shared.money import money

# 与 tasks/model.py 的 TASK_STATUSES 保持一致；这里只列出首页要数的四个
TASK_STATUSES = ("todo", "doing", "done", "cancelled")
# 「还算活着、会逾期」的状态：done/cancelled 不再计逾期，也不再进首页列表
ACTIVE_STATUSES = ("todo", "doing")
FOCUS_LIMIT = 5


def _task_scope(user_id: int) -> list[ColumnElement]:
    """tasks 的两条硬条件：限本人 + 未软删。所有 tasks 查询都从这里出发。"""
    return [Task.user_id == user_id, Task.deleted_at.is_(None)]


def _expense_scope(user_id: int) -> list[ColumnElement]:
    """expenses 的两条硬条件：限本人 + 未软删。"""
    return [Expense.user_id == user_id, Expense.deleted_at.is_(None)]


def _task_counts(
    db: Session,
    user_id: int,
    *,
    now_utc: datetime,
    tomorrow_start_utc: datetime,
) -> dict:
    """四个状态数 + 逾期 + 今日到期，两条 SQL。

    第 1 条按状态分组：一条 SQL 拿回全部，缺的状态在 Python 里补 0
    （比写四条 count 少三次往返，将来加状态也不用改查询）。

    第 2 条用条件聚合把「逾期」和「今日到期」一起数出来 ——
    它们扫的是同一批行、条件只是 due_at 与 now 的关系不同，拆开就是白读一遍表。
    用 COUNT(CASE WHEN ... THEN 1 END) 而不是 SUM(...ELSE 0)：
    count 只数非 NULL，空表时天然得 0，不用再 `or 0` 兜。

    逾期与今日到期**刻意不重叠**：
      逾期     due_at < 现在
      今日到期 due_at >= 现在 且 < 明天 00:00
    这样「逾期 4 + 今日到期 2」就是首页列表最多会出现的总条数，用户不会疑惑
    「为什么说逾期 4 条，列表里却有 6 条」。代价是今天上午已过点的待办只算逾期，
    不再算「今日到期」—— 卡片上两个数字相加才是重点数量。
    """
    rows = db.execute(
        select(Task.status, func.count()).where(*_task_scope(user_id)).group_by(Task.status)
    ).all()
    counts: dict[str, int] = {status: 0 for status in TASK_STATUSES}
    counts.update({status: int(n) for status, n in rows})

    overdue_expr = func.count(
        case(
            (
                (Task.due_at.is_not(None))
                & (Task.due_at < now_utc)
                & (Task.status.in_(ACTIVE_STATUSES)),
                1,
            )
        )
    )
    today_due_expr = func.count(
        case(
            (
                (Task.due_at >= now_utc)
                & (Task.due_at < tomorrow_start_utc)
                & (Task.status.in_(ACTIVE_STATUSES)),
                1,
            )
        )
    )
    overdue, today_due = db.execute(
        select(overdue_expr, today_due_expr).where(*_task_scope(user_id))
    ).one()
    counts["overdue"] = int(overdue or 0)
    counts["today_due"] = int(today_due or 0)
    return counts


def _expense_total(
    db: Session, user_id: int, *, start_utc: datetime, end_utc: datetime
) -> str:
    """某个区间内的支出合计（只算 expense，不算 income）。

    区间用半开 [start, end)：BETWEEN 两端都含，会把区间末尾整点那一笔算进来。
    三个区间（本月/上月/今日）**故意各发一条 SQL**：它们互不相交，
    硬合并成 SUM(CASE WHEN ...) 之后 WHERE 里就没有区间可走，
    (user_id, occurred_at) 索引会失效，等于扫这个人所有的账。
    """
    value = db.execute(
        select(func.sum(Expense.amount)).where(
            *_expense_scope(user_id),
            Expense.type == "expense",
            Expense.occurred_at >= start_utc,
            Expense.occurred_at < end_utc,
        )
    ).scalar()
    return money(value)


def _focus_tasks(
    db: Session, user_id: int, *, tomorrow_start_utc: datetime, limit: int = FOCUS_LIMIT
) -> list[Task]:
    """逾期与今日到期混排的待办，最多 limit 条。

    `due_at < 明天 00:00` 一条就同时覆盖「已逾期」和「今天到期」：
    逾期的时间点必然也早于明天 00:00，不需要再 OR 一个 due_at < now。

    `due_at IS NOT NULL` 是显式冗余（SQL 里 NULL < x 得 NULL，本来就不成立），
    写出来是为了让读的人不必在脑子里推一遍。

    第二排序键用 `id DESC`，与待办列表（tasks/crud.py 的 list_for_user）一致：
    同一个截止时间可能有多条，两处若各排各的，用户在首页点「查看全部」跳到待办页
    会发现那几条换了顺序。方向选 DESC 也是跟待办页对齐：同样紧急时，新记的在前。
    """
    rows = db.scalars(
        select(Task)
        .where(
            *_task_scope(user_id),
            Task.due_at.is_not(None),
            Task.due_at < tomorrow_start_utc,
            Task.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Task.due_at.asc(), Task.id.desc())
        .limit(limit)
    ).all()
    return list(rows)


def overview(
    db: Session, user_id: int, *, now_bj: datetime | None = None
) -> DashboardOverview:
    """首页全部数据，一次调用取齐。

    `now_bj` 由调用方传（router），这里不自己取当前时间：
    首页要同时算「今天」「本月」「近 7 天」，若每段各自取一次 now，
    跨零点的那一瞬间会出现「待办按今天算、消费按昨天算」。
    一次请求一个时间基准，是消费汇总里已经踩实的那条规矩。

    返回 pydantic 模型而不是 dict：这里所有数据都算完了，router 只做一次调用，
    没必要再到 router 里手工拼一遍。
    """
    if now_bj is None:
        now_bj = now_beijing()

    today: date = now_bj.date()
    now_utc = to_utc_naive(now_bj)  # 库里只存 naive UTC
    today_start_utc = beijing_day_start_utc(today)
    tomorrow_start_utc = beijing_day_start_utc(today + timedelta(days=1))

    month_start_utc, month_end_utc = beijing_month_range(today.year, today.month)
    last_year, last_month = shift_month(today.year, today.month, -1)
    last_start_utc, last_end_utc = beijing_month_range(last_year, last_month)

    days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]

    counts = _task_counts(
        db,
        user_id,
        now_utc=now_utc,
        tomorrow_start_utc=tomorrow_start_utc,
    )
    focus = _focus_tasks(db, user_id, tomorrow_start_utc=tomorrow_start_utc)

    return DashboardOverview(
        tasks=TaskStats(**counts),
        expense=ExpenseStats(
            current_month_total=_expense_total(
                db, user_id, start_utc=month_start_utc, end_utc=month_end_utc
            ),
            last_month_total=_expense_total(
                db, user_id, start_utc=last_start_utc, end_utc=last_end_utc
            ),
            today_total=_expense_total(
                db, user_id, start_utc=today_start_utc, end_utc=tomorrow_start_utc
            ),
        ),
        recent_7d=[DayTotal(**item) for item in daily_totals(db, user_id, days)],
        focus_tasks=[
            FocusTask(
                id=task.id,
                title=task.title,
                due_at=to_beijing_iso(task.due_at),
                status=task.status,
                # 与 tasks/model.py 的 Task.is_overdue 是同一口径，
                # 那里是给单条用，这里是给列表批量用；两处必须一起改。
                is_overdue=task.is_overdue(now_utc),
            )
            for task in focus
        ],
    )
