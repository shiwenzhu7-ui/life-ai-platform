"""消费接口 —— 只做三件事，不含业务规则。

  1. 绑定并校验 HTTP 参数（Query / Body）
  2. 注入 user_id（V1.0 固定演示用户；V1.2 换成 JWT 解析，本层不用改）
  3. 调 crud，把 ORM 对象转成响应 Schema

路由顺序提醒：`/expenses/summary`（第 7 步）是固定路径，
必须写在 `/{expense_id}` 之前，否则会被当成 id 解析、报 422 int_parsing。
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import get_current_user_id
from core.errors import validation_error
from core.time import now_utc, to_utc_naive
from modules.shared.categories import crud as categories_crud
from modules.shared.pagination import Page
from modules.lifeweb.expenses import crud
from modules.lifeweb.expenses.schema import ExpenseCreate, ExpenseOut, ExpenseType

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



@router.get("", response_model=Page[ExpenseOut], summary="消费列表")
def list_expenses(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    type: ExpenseType | None = Query(default=None, description="expense / income"),
    category_id: int | None = Query(default=None, ge=1, description="分类 ID"),
    keyword: str | None = Query(default=None, max_length=50, description="模糊匹配备注"),
    date_from: date | None = Query(
        default=None, description="发生日期起（含当天），YYYY-MM-DD"
    ),
    date_to: date | None = Query(
        default=None, description="发生日期止（含当天），YYYY-MM-DD"
    ),
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
