"""分类字典接口。

V1.0 只读（前端下拉框用）；新增/修改用户自建子类在 V1.4 开放。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_current_user_id
from app.db.session import get_db
from app.repositories.category_repo import CategoryRepository
from app.schemas.category import CategoryOut, CategoryScope, CategoryTree

router = APIRouter(prefix="/categories", tags=["分类"])


@router.get("", response_model=list[CategoryTree], summary="分类树（系统预置 + 本人自建）")
def list_categories(
    scope: CategoryScope = Query(..., description="用途域"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> list[CategoryTree]:
    categories = CategoryRepository(db).list_by_scope(scope, user_id)

    roots: list[CategoryTree] = []
    children_of: dict[int, list[CategoryOut]] = {}
    for item in categories:
        node = CategoryTree(
            id=item.id,
            scope=item.scope,
            name=item.name,
            parent_id=item.parent_id,
            is_system=bool(item.is_system),
            icon=item.icon,
            sort_order=item.sort_order,
            children=[],
        )
        if item.parent_id == 0:
            roots.append(node)
        else:
            children_of.setdefault(item.parent_id, []).append(
                CategoryOut(
                    id=item.id,
                    scope=item.scope,
                    name=item.name,
                    parent_id=item.parent_id,
                    is_system=bool(item.is_system),
                    icon=item.icon,
                    sort_order=item.sort_order,
                )
            )

    for root in roots:
        root.children = children_of.get(root.id, [])
    return roots
