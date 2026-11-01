"""消费模块的请求 / 响应模型。

`ExpenseCreate`（记一笔）与 `ExpenseOut`（列表/详情的单条）都在这里；
`ExpenseUpdate`（行内编辑）等第 5 步再补。
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.time import to_beijing_iso

# 与 model.EXPENSE_TYPES 和数据库 ENUM 三处一字不差
ExpenseType = Literal["expense", "income"]

# 业务上限 9999999.99（DECIMAL(12,2) 的列宽是 12 位总长，但业务只需要到千万级）
MAX_AMOUNT = Decimal("9999999.99")

# 单个标签最长 16 字符，一笔记账最多 10 个
Tag = Annotated[str, Field(max_length=16)]



class ExpenseCreate(BaseModel):
    """记一笔的请求体 —— 只放「前端能决定的东西」。

    刻意**没有**的字段：
      user_id      由登录态决定（core/deps.py），前端传了也不读
      source       manual / ai 由「哪个代码路径写入」决定，不由调用方声明
      created_at   updated_at   deleted_at   由数据库维护

    校验分两层，这里只管字段本身的格式：
      - 金额、长度、个数 → 本类（报 422 + detail.errors 指到具体字段）
      - 分类是否存在、域是否与 type 匹配 → 要查库，在 router._check_category
    """

    type: ExpenseType = Field(default="expense", description="expense / income")

    # decimal_places=2 直接拒绝「三位小数」，不做四舍五入 ——
    # 悄悄把用户敲的 12.345 改成 12.35，事后对账会对不上
    amount: Decimal = Field(
        gt=Decimal("0"),
        le=MAX_AMOUNT,
        max_digits=12,
        decimal_places=2,
        description="金额，正数；方向由 type 表达",
    )

    # 1 是合法的分类下界（分类 id 从 1 开始），0 在这里不是「未分类」而是非法
    category_id: int = Field(ge=1, description="分类 ID，域必须与 type 匹配")

    account: str = Field(default="现金", max_length=32, description="现金 / 微信 / 支付宝 / 银行卡")

    occurred_at: datetime = Field(description="发生时间；不带时区按北京时间解释")

    tags: list[Tag] | None = Field(
        default=None, max_length=10, description="最多 10 个标签"
    )

    note: str | None = Field(default=None, max_length=255, description="备注")

    @field_validator("tags", mode="before")
    @classmethod
    def clean_tags(cls, value):
        """去空白、去空串、去重（保持原顺序）。

        为什么在 mode="before"：先去干净，再让 max_length 校验「真正有意义的标签」个数。
        否则传 11 个「聚餐」会因为先去重、只算 1 个而通过，
        传 11 个不同的则会 422 —— 那才是我们要的行为。
        """
        if value is None:
            return None
        if not isinstance(value, list):
            return value
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned or None

    @field_validator("account")
    @classmethod
    def clean_account(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("账户不能为空")
        return text

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


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
