"""消费的数据访问层 —— 只写 SQL，不写业务规则。

两条铁律与待办模块完全一致：
  1. 必须带 user_id 过滤 —— 数据隔离的最后一道防线
  2. 必须排除软删除（deleted_at IS NULL）

排序与待办不同：待办按「到期时间升序」（越早越急），
消费按「发生时间**倒序**」（最近记的在上）——
对应 docs/页面设计.md §1.4 账单列表的顺序（11-01 在 10-31 之上）。
"""

from datetime import date, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from core.time import beijing_day_start_utc
from modules.lifeweb.expenses.model import Expense


def _base_query(user_id: int) -> Select:
    """所有查询的公共起点：限本人 + 未删除。"""
    return select(Expense).where(
        Expense.user_id == user_id, Expense.deleted_at.is_(None)
    )


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
        raw = (
            keyword.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        conds.append(Expense.note.like(f"%{raw}%", escape="\\"))

    if date_from:
        # 用户说的「11 月 1 日」= 北京时间那一整天（库里存 UTC）
        conds.append(Expense.occurred_at >= beijing_day_start_utc(date_from))

    if date_to:
        # 半开区间：包含当天 = < 次日 00:00（北京时间）
        conds.append(
            Expense.occurred_at < beijing_day_start_utc(date_to + timedelta(days=1))
        )

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
