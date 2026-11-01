"""统一分页结构。

所有列表接口都返回这个形状（见 docs/接口设计.md §1.2）：

    {"items": [...], "total": 42, "page": 1, "size": 20, "pages": 3}
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页响应。pages 由 total/size 算出来，不让前端自己算。"""

    items: list[T]
    total: int = Field(description="符合条件的总条数")
    page: int = Field(description="当前页码，从 1 开始")
    size: int = Field(description="每页条数")
    pages: int = Field(description="总页数")

    @classmethod
    def create(cls, items: list[T], total: int, page: int, size: int) -> "Page[T]":
        pages = (total + size - 1) // size if size > 0 else 0
        return cls(items=items, total=total, page=page, size=size, pages=pages)
