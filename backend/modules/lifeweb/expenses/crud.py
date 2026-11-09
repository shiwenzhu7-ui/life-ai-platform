"""消费的数据访问层 —— 只写 SQL，不写业务规则。

两条铁律与待办模块完全一致：
  1. 必须带 user_id 过滤 —— 数据隔离的最后一道防线
  2. 必须排除软删除（deleted_at IS NULL）

排序与待办不同：待办按「到期时间升序」（越早越急），
消费按「发生时间**倒序**」（最近记的在上）——
对应 docs/页面设计.md §1.4 账单列表的顺序（11-01 在 10-31 之上）。
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from core.time import beijing_day_start_utc, now_utc, to_utc_naive
from modules.lifeweb.expenses.model import EXPENSE_SOURCES, Expense
from modules.lifeweb.expenses.schema import ExpenseCreate, ExpenseUpdate
from modules.shared.money import money


def _base_query(user_id: int) -> Select:
    """所有查询的公共起点：限本人 + 未删除。"""
    return select(Expense).where(Expense.user_id == user_id, Expense.deleted_at.is_(None))


def list_for_user(
    db: Session,
    user_id: int,
    *,
    page: int = 1,
    size: int = 20,
    type: str | None = None,
    category_id: int | None = None,
    keyword: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[list[Expense], int]:
    """当前用户的收支列表（分页 + 筛选），返回 (当页数据, 总数)。

    筛选条件用 conds 列表累积、最后一次性 where(*conds)：
    加条件不用改 SQL 结构，也不会漏掉某个分支。

    total 必须按「筛选后」的条件算，而且要在 offset/limit 之前算 ——
    否则拿到的是「当页条数」，前端分页器会显示「共 20 条」。

    排序：occurred_at 倒序 + id 倒序兜底。第二级不能省 ——
    同一天记的两笔如果只按 occurred_at 排，数据库不保证相同排序键的返回顺序，
    翻页时同一条可能在两页里各出现一次，也可能一次都不出现。

    关键词只搜 note，不搜 tags：tags 是 JSON 列，`LIKE` 在 JSON 上不可靠，
    要做得用 JSON_CONTAINS 或全文索引，V1.0 先用最简单的东西顶上。
    """
    conds: list = []

    if type:
        conds.append(Expense.type == type)

    # 不能写 if category_id: —— 保持与待办一致的写法，
    # 避免以后出现「0 是有效值」时两个模块行为不一致
    if category_id is not None:
        conds.append(Expense.category_id == category_id)

    if keyword and keyword.strip():
        # 用户输入永远不能直接拼进 LIKE：% 和 _ 是通配符。
        # 顺序不能反 —— 必须先把 \ 换成 \\，否则后面加进去的转义符会被再转义一次。
        raw = keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append(Expense.note.like(f"%{raw}%", escape="\\"))

    if date_from:
        # 用户说的「11 月 1 日」= 北京时间那一整天（库里存 UTC）
        conds.append(Expense.occurred_at >= beijing_day_start_utc(date_from))

    if date_to:
        # 半开区间：包含当天 = < 次日 00:00（北京时间）
        conds.append(Expense.occurred_at < beijing_day_start_utc(date_to + timedelta(days=1)))

    base = _base_query(user_id)
    if conds:
        base = base.where(*conds)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    stmt = (
        base.order_by(Expense.occurred_at.desc(), Expense.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    items = list(db.scalars(stmt).all())
    return items, int(total)


def get_for_user(db: Session, user_id: int, expense_id: int) -> Expense | None:
    """取一笔账；取不到返回 None，由 router 决定怎么回（这里不抛 HTTP 异常）。

    复用 _base_query 是关键，它自带两个条件：
      - user_id == 当前用户：别人的记录查不到 → router 回 404（不是 403，
        403 等于告诉对方「这条数据存在，只是你没权限」，能被用来探测别人有多少条）
      - deleted_at IS NULL：已软删的记录查不到 → 也是 404
    """
    return db.scalars(_base_query(user_id).where(Expense.id == expense_id)).first()


def create(
    db: Session,
    user_id: int,
    payload: ExpenseCreate,
    *,
    source: str = "manual",
    commit: bool = True,
) -> Expense:
    """记一笔。

    四个要点：
      1. user_id 由调用方（router 从登录态）传进来，不读请求体
      2. source 由**哪条代码路径**决定，不由请求体声明：手工记账是 manual，
         AI 聊天的确认卡片是 ai（A8）。前端传了也不认。
      3. occurred_at 入库前转成 naive UTC（库里只存 UTC）
      4. commit 之后的 refresh 不能省：id / created_at / updated_at
         都是数据库生成的，不 refresh 拿回来是 None，响应里会缺字段

    `commit=False` 是给 AI 确认卡片用的（同 tasks/crud.create）：这笔账必须和
    「卡片置 confirmed」在同一个事务里提交，否则会留下「账记上了、卡片还能再点」
    的重复窗口。那种情况下也不 refresh —— 事务还没提交，拿不到数据库生成的值。
    """
    if source not in EXPENSE_SOURCES:
        raise ValueError(f"未知来源 {source!r}，只能是 {EXPENSE_SOURCES}")

    expense = Expense(
        user_id=user_id,
        type=payload.type,
        amount=payload.amount,
        category_id=payload.category_id,
        account=payload.account,
        occurred_at=to_utc_naive(payload.occurred_at),
        tags=payload.tags,
        note=payload.note,
        source=source,
    )
    db.add(expense)
    if not commit:
        db.flush()  # 拿到自增 id 就够；提交由调用方统一做
        return expense

    db.commit()
    db.refresh(expense)
    return expense


def update(db: Session, expense: Expense, payload: ExpenseUpdate) -> Expense:
    """局部更新：只把「请求里真的出现过」的字段写到对象上。

    exclude_unset=True 是这一步的核心：
      model_dump()                    → 把所有字段都导出（没传的变成 None）→ 会把备注清空
      model_dump(exclude_unset=True)  → 只导出现过的键 → 没传的字段保持原值

    判据是 Pydantic 的 model_fields_set。注意 {"note": null} 与 {} 的属性值都是
    None，区别只在 fields_set —— 所以不能靠「值是不是 None」判断有没传。

    updated_at 不用手动写：model 里那一列带 onupdate=func.now()，
    这次 UPDATE 只要有列变化，数据库自己会刷新它。
    """
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if key == "occurred_at":
            value = to_utc_naive(value)  # 库里只存 naive UTC
        setattr(expense, key, value)

    db.commit()
    db.refresh(expense)
    return expense


def soft_delete(db: Session, expense: Expense) -> None:
    """软删除：只把 deleted_at 打上时间戳，那一行仍留在表里。

    只写一列，不碰 amount / type / 其它字段 —— 用户点「删除」的意思是
    「这条不该再出现」，不是「把金额改成 0」。

    故意不 refresh：调用方（DELETE 接口）返回 204 无响应体，
    没有人读这个对象的新状态，多一次 SELECT 是白花。

    为什么不用物理删：删错了无法恢复；也统计不出「这个月删了几条」。
    代价是表会慢慢变大 —— 以后加一个「deleted_at 超过 90 天就物理删」的清理任务。
    """
    expense.deleted_at = now_utc()
    db.commit()


def daily_totals(db: Session, user_id: int, days: list[date]) -> list[dict]:
    """若干天的每日支出合计，缺的日期补 "0.00"，返回顺序与 days 一致。

    两个消费者，要求完全一样，所以只有这**一份**实现：
      `GET /expenses/summary` 的 recent_7d
      `GET /dashboard/overview` 的 recent_7d（首页迷你柱状图）

    抽出来的收益不是少几行，而是**口径只有一处**：只算支出、只算未删、
    按北京日期切日、缺的补零。任何一条各写一遍都会慢慢分叉。

    days 必须是**升序且连续**的日期（调用方负责）：这里只取两头
    [days[0] 00:00, days[-1]+1 00:00) 做半开区间，中间的空洞靠 Python 补零，
    不必生成完整的日期序列参数。
    """
    if not days:
        return []

    # CONVERT_TZ 用数字偏移 "+08:00"，不用 "Asia/Shanghai"：
    # 后者要求 MySQL 装了时区表（mysql.time_zone_name），部署时最容易缺的就是它。
    local_day = func.date(func.convert_tz(Expense.occurred_at, "+00:00", "+08:00"))
    rows = db.execute(
        select(local_day, func.sum(Expense.amount))
        .where(
            Expense.user_id == user_id,
            Expense.deleted_at.is_(None),
            Expense.type == "expense",
            Expense.occurred_at >= beijing_day_start_utc(days[0]),
            Expense.occurred_at < beijing_day_start_utc(days[-1] + timedelta(days=1)),
        )
        .group_by(local_day)
    ).all()

    daily = {str(day): money(total) for day, total in rows}

    # 没记账的那天补 "0.00"：前端画图不用处理缺日，点数永远齐全
    return [
        {"date": day.isoformat(), "expense": daily.get(day.isoformat(), "0.00")} for day in days
    ]


def summarize(
    db: Session,
    user_id: int,
    *,
    month_start_utc: datetime,
    month_end_utc: datetime,
    days: list[date],
) -> dict:
    """月度汇总：两个总额 + 分类占比 + 最近 7 天按天合计。

    返回的是**普通字典**（金额已格式化成两位小数的字符串），不是 ORM 对象 ——
    聚合查询的结果本来就不是某张表的行，套不回 Expense。
    分类名要查 categories 表，由 router 用 name_map 补上（本层不跨模块查表）。

    三个查询都必须自己带 user_id 与 deleted_at IS NULL：这里没法复用
    _base_query —— 它返回的是 Expense 实体查询，而这里要的是「按 type 分组」的聚合列。
    忘了 deleted_at 那一行，用户删掉的账会继续被算进总额。
    """
    # 当月的公共条件：区间是半开的，见 core/time.py 的 beijing_month_range
    month_conds = [
        Expense.user_id == user_id,
        Expense.deleted_at.is_(None),
        Expense.occurred_at >= month_start_utc,
        Expense.occurred_at < month_end_utc,
    ]

    # ① 两个总额：一条 SQL 分组拿回来，只有支没有收时 income 那个键不存在
    totals = dict(
        db.execute(
            select(Expense.type, func.sum(Expense.amount))
            .where(*month_conds)
            .group_by(Expense.type)
        ).all()
    )
    expense_total = Decimal(totals.get("expense") or 0)
    income_total = Decimal(totals.get("income") or 0)

    # ② 分类占比：只统计支出。收入没有「品类占比」这回事，
    #    混进来会让饼图的分母失去意义。
    category_rows = db.execute(
        select(
            Expense.category_id,
            func.sum(Expense.amount),
            func.count(),
        )
        .where(*month_conds, Expense.type == "expense")
        .group_by(Expense.category_id)
        .order_by(func.sum(Expense.amount).desc())
    ).all()

    by_category = [
        {
            "category_id": category_id,
            "total": money(total),
            # 支出为 0 时不能除零，直接给 0.0（此时也不可能有分组行，属双保险）
            "ratio": (round(float(Decimal(total) / expense_total), 3) if expense_total else 0.0),
            "count": int(count),
        }
        for category_id, total, count in category_rows
    ]

    # ③ 最近 7 天：与 month 无关，永远是「今天往前 7 天（含今天）」。
    #    具体实现已抽到 daily_totals()：首页的迷你柱状图要的是同一份数据，
    #    两处共用一条 SQL，才不会出现「消费页补零、首页不补零」这种偏差。
    recent_7d = daily_totals(db, user_id, days)

    return {
        "expense_total": money(expense_total),
        "income_total": money(income_total),
        "balance": money(income_total - expense_total),
        "by_category": by_category,
        "recent_7d": recent_7d,
    }
