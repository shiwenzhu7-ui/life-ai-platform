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

from core.time import beijing_day_start_utc, now_utc, to_utc_naive
from modules.lifeweb.expenses.model import Expense
from modules.lifeweb.expenses.schema import ExpenseCreate, ExpenseUpdate


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


def get_for_user(db: Session, user_id: int, expense_id: int) -> Expense | None:
    """取一笔账；取不到返回 None，由 router 决定怎么回（这里不抛 HTTP 异常）。

    复用 _base_query 是关键，它自带两个条件：
      - user_id == 当前用户：别人的记录查不到 → router 回 404（不是 403，
        403 等于告诉对方「这条数据存在，只是你没权限」，能被用来探测别人有多少条）
      - deleted_at IS NULL：已软删的记录查不到 → 也是 404
    """
    return db.scalars(_base_query(user_id).where(Expense.id == expense_id)).first()


def create(db: Session, user_id: int, payload: ExpenseCreate) -> Expense:
    """记一笔。

    四个要点：
      1. user_id 由调用方（router 从登录态）传进来，不读请求体
      2. source 固定 manual —— 这条路径只承载「用户手记」，
         V1.1 的 AI 记账走另一条路径写 'ai'，前端无权声明来源
      3. occurred_at 入库前转成 naive UTC（库里只存 UTC）
      4. commit 之后的 refresh 不能省：id / created_at / updated_at
         都是数据库生成的，不 refresh 拿回来是 None，响应里会缺字段
    """
    expense = Expense(
        user_id=user_id,
        type=payload.type,
        amount=payload.amount,
        category_id=payload.category_id,
        account=payload.account,
        occurred_at=to_utc_naive(payload.occurred_at),
        tags=payload.tags,
        note=payload.note,
        source="manual",
    )
    db.add(expense)
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

