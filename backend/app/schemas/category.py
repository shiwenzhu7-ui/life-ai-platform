"""分类字典 Schema。"""

from typing import Literal

from pydantic import BaseModel

CategoryScope = Literal["expense", "income", "task", "shopping", "diet", "exercise", "mood"]


class CategoryOut(BaseModel):
    id: int
    scope: str
    name: str
    parent_id: int
    is_system: bool
    icon: str | None = None
    sort_order: int


class CategoryTree(CategoryOut):
    """两级分类树：一级分类带 children（用户自建的二级子类）。"""

    children: list[CategoryOut] = []
