"""消费模块的请求 / 响应模型。

这一步只有 `ExpenseOut`（列表用）；`ExpenseCreate` / `ExpenseUpdate`
等第 3、5 步再补 —— 一个接口一个接口来，不写暂时没人用的类。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.time import to_beijing_iso

# 与 model.EXPENSE_TYPES 和数据库 ENUM 三处一字不差
ExpenseType = Literal["expense", "income"]


class ExpenseOut(BaseModel):
    """一条收支记录的对外形状。

    两个刻意的选择：
      amount 是**字符串**（"1280.50"）而不是浮点：docs/接口设计.md §5 要求
              「金额统一以字符串返回，避免浮点误差」，前端只负责显示。
      occurred_at 也输出字符串并带 +08:00：库里存 UTC，对外一律北京时间，
              全站只允许 core/time.py 做时区换算。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    type: ExpenseType
    amount: str
    category_id: int
    category_name: str | None = None
    account: str
    occurred_at: str
    tags: list[str] | None = None
    note: str | None = None
    source: str

    @classmethod
    def from_model(
        cls, expense, category_names: dict[int, str] | None = None
    ) -> "ExpenseOut":
        """ORM 对象 → 响应模型。

        category_names 是整页共用的一张 {id: 名字}（见 categories.crud.name_map），
        传进来就不必每条记录再查一次库。
        """
        names = category_names or {}
        return cls(
            id=expense.id,
            type=expense.type,
            # Decimal("1280.50") → "1280.50"，小数位数由 DECIMAL(12,2) 保证
            amount=str(expense.amount),
            category_id=expense.category_id,
            category_name=names.get(expense.category_id),
            account=expense.account,
            occurred_at=to_beijing_iso(expense.occurred_at),
            tags=expense.tags,
            note=expense.note,
            source=expense.source,
        )
