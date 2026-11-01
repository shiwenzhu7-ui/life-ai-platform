"""categories 的出入参。

V1.0 只需要出参（只读）。写接口 V1.4 再开，那时才加 CategoryCreate。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.shared.categories.model import Category

# 与 model.CATEGORY_SCOPES 一字不差（那边是数据库 ENUM，这边是接口校验）
CategoryScope = Literal["expense", "income", "task", "shopping", "diet", "exercise", "mood"]


class CategoryOut(BaseModel):
    """一个分类。

    只有 id / scope / name / parent_id / icon / sort_order 这些前端画下拉框要用的字段；
    owner_user_id、is_system 这类内部字段不返回 —— 前端无法据此做任何决策，
    返回了反而会被误用成权限判断。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    scope: str = Field(description="用途域：expense/income/task/...")
    name: str
    parent_id: int = Field(description="0=一级分类")
    icon: str | None = None
    sort_order: int

    @classmethod
    def from_model(cls, category: Category) -> "CategoryOut":
        return cls.model_validate(category)
