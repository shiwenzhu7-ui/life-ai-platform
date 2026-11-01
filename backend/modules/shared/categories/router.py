"""分类字典接口（三端共用，V1.0 只读）。

只有这一个接口：GET /categories。docs/接口设计.md §6 写明
POST / PATCH / DELETE 是 V1.4 的事，现在一个都不做 ——
接口先开出来、实现是空的，比没有更难处理。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import get_current_user_id
from modules.shared.categories import crud
from modules.shared.categories.schema import CategoryOut, CategoryScope

router = APIRouter(prefix="/categories", tags=["分类"])


@router.get("", response_model=list[CategoryOut], summary="分类列表（系统 + 本人自建）")
def list_categories(
    scope: CategoryScope | None = Query(
        default=None, description="用途域；不传则返回全部用途域"
    ),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> list[CategoryOut]:
    """按用途域取分类。

    不分页：41 条系统分类 + 少量自建，一次全给比翻页省事，前端也不用处理 page。
    V1.0 的数据全部是一级分类（parent_id=0），返回的是**平表**；
    等 V1.4 允许用户建二级子类时，再在这里按 parent_id 组装成树。
    """
    rows = crud.list_visible(db, user_id, scope=scope)
    return [CategoryOut.from_model(row) for row in rows]
