"""消费模块的请求 / 响应模型。

`ExpenseCreate`（记一笔）、`ExpenseUpdate`（改一笔）、`ExpenseOut`（列表/详情/写操作的单条）
都在这里。
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.time import to_beijing_iso

# 与 model.EXPENSE_TYPES 和数据库 ENUM 三处一字不差
ExpenseType = Literal["expense", "income"]

# 业务上限 9999999.99（DECIMAL(12,2) 的列宽是 12 位总长，但业务只需要到千万级）
MAX_AMOUNT = Decimal("9999999.99")

# 单个标签最长 16 字符，一笔记账最多 10 个
Tag = Annotated[str, Field(max_length=16)]

# PATCH 时「允许传 null 表示清空」的字段；其余字段传 null 一律 422。
# 依据是数据库列的可空性（见 modules/lifeweb/expenses/model.py）：只有 tags / note 可空。
NULLABLE_FIELDS = ("tags", "note")


def _clean_tags(value):
    """去空白、去空串、去重（保持原顺序）；结果为空则返回 None。

    放在模块级是因为 ExpenseCreate 与 ExpenseUpdate 共用这一份规则 ——
    两边各写一份，早晚会有一边改了另一边没改。
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


def _clean_account(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("账户不能为空")
    return text


def _clean_note(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


class ExpenseCreate(BaseModel):
    """记一笔的请求体 —— 只放「前端能决定的东西」。

    刻意**没有**的字段：
      user_id      由登录态决定（core/deps.py），前端传了也不读
      source       manual / ai 由「哪个代码路径写入」决定，不由调用方声明
      created_at   updated_at              由数据库维护

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

    tags: list[Tag] | None = Field(default=None, max_length=10, description="最多 10 个标签")

    note: str | None = Field(default=None, max_length=255, description="备注")

    @field_validator("tags", mode="before")
    @classmethod
    def clean_tags(cls, value):
        """先去干净，再让 max_length 校验「真正有意义的标签」个数。

        顺序不能反：否则传 11 个「聚餐」会因为先去重、只算 1 个而通过，
        传 11 个不同的却会 422 —— 那才是我们要的行为。
        """
        return _clean_tags(value)

    @field_validator("account")
    @classmethod
    def clean_account(cls, value: str) -> str:
        return _clean_account(value)

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str | None) -> str | None:
        return _clean_note(value)


class ExpenseUpdate(BaseModel):
    """改一笔的请求体 —— 每个字段都可省略，省略就表示「保持原样」。

    三态语义（全靠 Pydantic 的 model_fields_set 区分，不需要额外标记）：
      没出现该键        → 不动这个字段
      出现且值为 null   → 只有 NULLABLE_FIELDS（tags / note）允许，表示清空；其余 422
      出现且有值        → 改成这个值

    为什么不能复用 ExpenseCreate 继承：继承会把 amount / category_id / occurred_at
    的「必填」一起带过来，而 PATCH 这三个都是可选的。

    刻意**没有**的字段：
      id / user_id   身份，改了就是越权
      source         由「哪条代码路径写入」决定，PATCH 能改就等于伪造来源
      created_at / updated_at   数据库维护
    """

    type: ExpenseType | None = Field(default=None, description="expense / income")

    amount: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        le=MAX_AMOUNT,
        max_digits=12,
        decimal_places=2,
        description="金额，正数；两位小数",
    )

    category_id: int | None = Field(default=None, ge=1, description="分类 ID，域须与 type 匹配")

    account: str | None = Field(
        default=None, max_length=32, description="现金 / 微信 / 支付宝 / 银行卡"
    )

    occurred_at: datetime | None = Field(
        default=None, description="发生时间；不带时区按北京时间解释"
    )

    tags: list[Tag] | None = Field(
        default=None, max_length=10, description="最多 10 个标签；null 清空"
    )

    note: str | None = Field(default=None, max_length=255, description="备注；null 清空")

    @field_validator("tags", mode="before")
    @classmethod
    def clean_tags(cls, value):
        # 传 [] 也走这里：清洗后为空 → None → 落库就是「没有标签」，
        # 与 POST 里「标签全空存 null」保持同一种表示
        return _clean_tags(value)

    @field_validator("account")
    @classmethod
    def clean_account(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_account(value)

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str | None) -> str | None:
        return _clean_note(value)

    @model_validator(mode="after")
    def check_patch_fields(self) -> "ExpenseUpdate":
        """两条整模型规则：至少要改一个字段；非空字段不许传 null。

        为什么整条 `{}` 要报错：一个什么都没改的 PATCH 通常是前端的 bug
        （表单没绑定、按钮传错了对象），静默 200 会让这种 bug 永远查不出来。
        """
        if not self.model_fields_set:
            raise ValueError("没有需要修改的字段")

        for name in self.model_fields_set - set(NULLABLE_FIELDS):
            if getattr(self, name) is None:
                raise ValueError(f"{name} 不能为 null")

        return self


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
    def from_model(cls, expense, category_names: dict[int, str] | None = None) -> "ExpenseOut":
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


class CategoryShare(BaseModel):
    """周汇总里的一行「某分类花了多少」。

    三个刻意的选择：
      ratio 的分母是**支出总额**（不是收支合计）：它表达「这张饼的哪一块」，
            只对支出有意义；收入那边没有「占比」这回事。
      ratio 保留 3 位小数而不是原样输出：前端显示的百分比是 "48.4%"，
            多出来的位数没有意义，反而会让浮点尾数泄漏到界面上。
      name 可能为 null：分类被删（物理删）之后历史账仍在，
            这时让前端显示「未命名」比让整页 500 好。
    """

    category_id: int
    category_name: str | None = None
    total: str
    ratio: float
    count: int


class DayTotal(BaseModel):
    """最近 7 天里的某一天：date 是北京时间的日期，expense 是当天支出合计。"""

    date: str
    expense: str


class ExpenseSummary(BaseModel):
    """月度汇总的响应体 —— 形状见 docs/接口设计.md §5。

    四个金额全是字符串且恒有两位小数（"0.00" 而不是 "0"）：
    前端直接显示，不参与计算，避免 JS 的浮点在界面上露出来。

    by_category 与 recent_7d 恒为**数组**（没有数据就是 []），不会是 null ——
    前端可以直接 .map()/.length，不用先判空。
    """

    period: str
    expense_total: str
    income_total: str
    balance: str
    by_category: list[CategoryShare]
    recent_7d: list[DayTotal]
