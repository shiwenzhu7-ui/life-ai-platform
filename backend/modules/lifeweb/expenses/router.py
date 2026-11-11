"""消费接口 —— 只做三件事，不含业务规则。

  1. 绑定并校验 HTTP 参数（Query / Body）
  2. 注入 user_id（V1.0 固定演示用户；V1.2 换成 JWT 解析，本层不用改）
  3. 调 crud，把 ORM 对象转成响应 Schema

路由顺序提醒：`/expenses/summary`（第 7 步）是固定路径，
必须写在 `/{expense_id}` 之前，否则会被当成 id 解析、报 422 int_parsing。
"""

import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import get_current_user_id
from core.errors import not_found, validation_error
from core.time import beijing_month_range, now_beijing, now_utc, to_utc_naive
from modules.lifeweb.expenses import crud
from modules.lifeweb.expenses.model import Expense
from modules.lifeweb.expenses.schema import (
    ExpenseCreate,
    ExpenseOut,
    ExpenseSummary,
    ExpenseType,
    ExpenseUpdate,
)
from modules.shared.categories import crud as categories_crud
from modules.shared.pagination import Page

router = APIRouter(prefix="/expenses", tags=["消费"])

# 允许「提前记账」的窗口：文档 §5 写的是「不能晚于当前时间 + 1 天」，
# 用途是「明天的聚餐先记上」这类场景
FUTURE_DAYS = 1


def _check_category(db: Session, user_id: int, category_id: int, *, scope: str) -> None:
    """校验分类能用于这笔账：必须存在、scope 与 type 一致、属于本人或系统。

    与待办模块的 _check_category 的差别只有一个参数：
    这里的 scope 跟着请求体里的 type 走（expense / income），
    待办那边是固定的 "task"。

    报 422 而不是 404：这是请求体里某个字段不合法（前端要提示「分类不能用于收入」），
    不是「你要访问的资源不存在」。
    """
    if not categories_crud.exists_visible(db, user_id, category_id, scope=scope):
        raise validation_error("分类不存在")


def _check_occurred_at(occurred_at) -> None:
    """发生时间不能晚于「当前时间 + 1 天」。

    顺序必须是先 to_utc_naive 再比较：请求里的时间可能带时区也可能不带，
    而 now_utc() 是 naive UTC —— aware 与 naive 直接比较会抛 TypeError（500）。
    """
    limit = now_utc() + timedelta(days=FUTURE_DAYS)
    if to_utc_naive(occurred_at) > limit:
        raise validation_error(f"发生时间不能晚于当前时间 {FUTURE_DAYS} 天")


def _get_or_404(db: Session, user_id: int, expense_id: int) -> Expense:
    """取一笔账，取不到就 404 —— 详情 / 修改 / 删除三个接口共用这一个入口。

    统一在这里抛，好处是「别人记录的 404」和「已软删记录的 404」只写一次，
    三个接口不可能各自漏掉一个条件。
    """
    expense = crud.get_for_user(db, user_id, expense_id)
    if expense is None:
        raise not_found("收支记录不存在")
    return expense


@router.get("", response_model=Page[ExpenseOut], summary="消费列表")
def list_expenses(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    type: ExpenseType | None = Query(default=None, description="expense / income"),
    category_id: int | None = Query(default=None, ge=1, description="分类 ID"),
    keyword: str | None = Query(default=None, max_length=50, description="模糊匹配备注"),
    date_from: date | None = Query(default=None, description="发生日期起（含当天），YYYY-MM-DD"),
    date_to: date | None = Query(default=None, description="发生日期止（含当天），YYYY-MM-DD"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Page[ExpenseOut]:
    """账单列表 + 筛选，默认按发生时间倒序。

    未声明的查询参数会被 FastAPI 静默忽略（照样 200，只是没筛），
    所以 docs/页面设计.md §1.4 里出现的每个参数都必须在这里显式声明。
    """
    if date_from and date_to and date_from > date_to:
        raise validation_error("date_from 不能晚于 date_to")

    items, total = crud.list_for_user(
        db,
        user_id,
        page=page,
        size=size,
        type=type,
        category_id=category_id,
        keyword=keyword,
        date_from=date_from,
        date_to=date_to,
    )
    names = categories_crud.name_map(db)  # 整页共用一张 {id: 名字}，不做 N+1
    return Page.create(
        [ExpenseOut.from_model(item, names) for item in items],
        total,
        page,
        size,
    )


@router.post("", response_model=ExpenseOut, status_code=201, summary="记一笔")
def create_expense(
    payload: ExpenseCreate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ExpenseOut:
    """新增一笔收支。

    能走到这里，说明金额、长度、标签个数这些**字段格式**已经过了 Pydantic；
    还有两件需要查库/看时钟的事在这里做：
      1. 发生时间不能晚于「当前时间 + 1 天」
      2. 分类必须存在，且域与 type 一致（expense 只能选 expense 域的分类）

    响应 201 + 完整的一条（形状与列表里的单条一致）：
    前端可以直接把它插到列表第一行，不用重新拉整个列表。
    """
    _check_occurred_at(payload.occurred_at)
    _check_category(db, user_id, payload.category_id, scope=payload.type)

    expense = crud.create(db, user_id, payload)
    return ExpenseOut.from_model(expense, categories_crud.name_map(db))


# 注意：/expenses/summary（第 7 步）是固定路径，必须写在这里 —— 在 /{expense_id} 之前，
# 否则 "summary" 会被当成 id 解析成 int，直接 422 int_parsing。

# 只接受 "2026-11" 这种两位月份；"2026-1" / "2026-13" / "abc" 一律 422
_MONTH_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")


def _parse_month(month: str | None) -> tuple[str, int, int]:
    """把 "2026-11" 拆成 ("2026-11", 2026, 11)；不传就取北京时间的当月。

    为什么用正则而不用 strptime("%Y-%m")：strptime 会把 "2026-1" 也吃下来，
    而接口约定（docs/接口设计.md §5 的 period 示例）是两位月份。
    校验放在这里而不是 Query(pattern=...)：这样报的是我们的统一错误体，
    前端只需要处理一种 422 形状。
    """
    if month is None:
        today = now_beijing().date()
        return f"{today.year:04d}-{today.month:02d}", today.year, today.month

    matched = _MONTH_PATTERN.match(month.strip())
    if matched is None:
        raise validation_error("month 格式应为 YYYY-MM")

    year = int(matched.group("year"))
    mon = int(matched.group("month"))
    return f"{year:04d}-{mon:02d}", year, mon


@router.get("/summary", response_model=ExpenseSummary, summary="月度汇总")
def get_summary(
    month: str | None = Query(default=None, description="月份 YYYY-MM，默认当月"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ExpenseSummary:
    """一个月的收支汇总 —— 首页的消费卡片和消费页顶部的三个数字都用它。

    三块数据的口径：
      1. 两个总额与 balance：只统计「这个月」+「未删除」的账
      2. by_category：只统计支出，ratio 的分母是支出总额
      3. recent_7d：与 month 无关，永远是「今天往前 7 天（含今天）」，缺日补 0.00

    日期边界全部用北京时间的半开区间（见 core/time.py 的 beijing_month_range）：
    month=2026-11 对应库里 UTC 的 [2026-10-31 16:00, 2026-11-30 16:00)。
    """
    period, year, mon = _parse_month(month)
    month_start, month_end = beijing_month_range(year, mon)

    # 最近 7 天：今天往前数 6 天，连今天共 7 个，升序（今天在最后）
    today = now_beijing().date()
    days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]

    data = crud.summarize(
        db,
        user_id,
        month_start_utc=month_start,
        month_end_utc=month_end,
        days=days,
    )

    # 分类名整页查一次（不做 N+1）；crud 不跨模块查 categories 表
    names = categories_crud.name_map(db)
    for item in data["by_category"]:
        item["category_name"] = names.get(item["category_id"])

    return ExpenseSummary(period=period, **data)


@router.get("/{expense_id}", response_model=ExpenseOut, summary="一笔的详情")
def get_expense(
    expense_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ExpenseOut:
    """单笔详情，形状与列表里的单条**完全相同**。

    谁在用：消费页点某行的 ✏ 时，把详情回填进「改一笔」抽屉。
    为什么不用列表里那一行凑：列表是当前页的快照，详情是此刻的真实状态 ——
    用快照回填，保存时就会拿旧值覆盖别人刚改的新值。

    路径参数 expense_id 写 `int` 就够了：`/expenses/abc` 由 FastAPI 在进函数前
    拦成 422（loc = ["path", "expense_id"]），不用手写类型判断。
    """
    expense = _get_or_404(db, user_id, expense_id)
    return ExpenseOut.from_model(expense, categories_crud.name_map(db))


@router.patch("/{expense_id}", response_model=ExpenseOut, summary="改一笔")
def update_expense(
    expense_id: int,
    payload: ExpenseUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ExpenseOut:
    """局部更新：只改请求里出现的字段，其余保持原样。

    两件要查库/看时钟的事，且**只看出现过的字段**：
      1. 传了 occurred_at → 重新检查「不能晚于当前时间 + 1 天」
      2. 传了 type 或 category_id → 用「合并后的最终状态」重新检查分类域

    第 2 条里“合并”是关键：只传一半时必须拿库里的旧值补齐，否则
    「库里的 income/工资，请求只传 category_id=1（餐饮）」会被误判成合法。
    """
    expense = _get_or_404(db, user_id, expense_id)
    changed = payload.model_fields_set

    if "occurred_at" in changed:
        _check_occurred_at(payload.occurred_at)

    if changed & {"type", "category_id"}:
        final_type = payload.type if "type" in changed else expense.type
        final_category = payload.category_id if "category_id" in changed else expense.category_id
        _check_category(db, user_id, final_category, scope=final_type)

    expense = crud.update(db, expense, payload)
    return ExpenseOut.from_model(expense, categories_crud.name_map(db))


@router.delete("/{expense_id}", status_code=204, summary="删一笔")
def delete_expense(
    expense_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> None:
    """删除一笔账（硬删，行真的从表里消失）。

    两个 204 必须同时满足的细节（少一个，运行到请求时才报
    "Status code 204 must not have a body"）：
      1. 装饰器里写 status_code=204
      2. **不写 response_model**，且返回类型标注 None、真的不 return 任何东西

    第二次删同一条会自然 404：_get_or_404 走 crud.get_for_user，
    第一次删完行就没了，查不到。
    """
    expense = _get_or_404(db, user_id, expense_id)
    crud.delete(db, expense)
