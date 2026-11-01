"""消费接口 —— 只做三件事，不含业务规则。

  1. 绑定并校验 HTTP 参数（Query / Body）
  2. 注入 user_id（V1.0 固定演示用户；V1.2 换成 JWT 解析，本层不用改）
  3. 调 crud，把 ORM 对象转成响应 Schema

路由顺序提醒：`/expenses/summary`（第 7 步）是固定路径，
必须写在 `/{expense_id}` 之前，否则会被当成 id 解析、报 422 int_parsing。
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import get_current_user_id
from core.errors import validation_error
from modules.shared.categories import crud as categories_crud
from modules.shared.pagination import Page
from modules.lifeweb.expenses import crud
from modules.lifeweb.expenses.schema import ExpenseOut, ExpenseType

router = APIRouter(prefix="/expenses", tags=["消费"])


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
