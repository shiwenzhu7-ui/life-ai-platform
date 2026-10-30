"""分类字典数据访问层。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.category import Category


class CategoryRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, category_id: int) -> Category | None:
        return self.db.get(Category, category_id)

    def list_by_scope(self, scope: str, user_id: int) -> list[Category]:
        """某用途域下的可见分类：系统预置（owner_user_id=0）+ 本人自建。"""
        stmt = (
            select(Category)
            .where(
                Category.scope == scope,
                Category.deleted_at.is_(None),
                Category.owner_user_id.in_((0, user_id)),
            )
            .order_by(Category.parent_id.asc(), Category.sort_order.asc(), Category.id.asc())
        )
        return list(self.db.scalars(stmt).all())

    def name_map(self, scope: str, user_id: int) -> dict[int, str]:
        """id → 名称的映射，用于列表一次性补 category_name，避免 N+1 查询。"""
        return {c.id: c.name for c in self.list_by_scope(scope, user_id)}

    def exists_in_scope(self, category_id: int, scope: str, user_id: int) -> bool:
        """校验分类存在、用途域匹配、且属于系统预置或本人。"""
        if category_id <= 0:
            return True  # 0 表示「未分类」，合法
        stmt = select(Category.id).where(
            Category.id == category_id,
            Category.scope == scope,
            Category.deleted_at.is_(None),
            Category.owner_user_id.in_((0, user_id)),
        )
        return self.db.scalar(stmt) is not None
