"""购物模块的请求 / 响应模型。

和待办（tasks）最大的不同：这里是**父子两层**。

    shopping_lists（清单）  1 ── N  shopping_items（明细）

所以模型分成两族：
    清单族  ListCreate / ListUpdate / ListOut
    明细族  ItemCreate / ItemUpdate / ItemPurchased / ItemOut

几条贯穿全文件的约定（和待办、消费保持一致，不另起炉灶）：
  - user_id 永远不出现在入参里 —— 它由令牌决定，写进请求体就等于允许
    往别人账户塞数据（待办 schema 的 docstring 已经踩过这个坑）
  - 时间一律经 core/time.py 转成带 +08:00 的 ISO 字符串
  - quantity 入库用 Decimal，出参用**字符串**，理由同消费模块的 amount：
    JSON 的 number 是双精度浮点，2.5 这种数看起来没事，但
    "0.1 + 0.2" 一类的账目在前端累加会飘；字符串让前端只负责显示
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.time import to_beijing_iso

# 与 model.SHOPPING_LIST_STATUSES 和数据库 ENUM 三处一字不差
ListStatus = Literal["active", "archived"]

# 物品来源：manual = 页面手加，ai = 助手确认后写入
ItemSource = Literal["manual", "ai"]

# 数量上限：数据库是 DECIMAL(10,2)，业务上真正会用到的顶格是「9999.99」
# （一次性买 10000 件东西不是记账，是进货）。比列宽紧一档，是为了让
# 越界在 Pydantic 层就变成 422，而不是等到 MySQL 报 Out of range（→500）。
MAX_QUANTITY = Decimal("9999.99")

# PATCH 时「允许传 null 表示清空」的字段；其余字段传 null 一律 422。
# 依据是数据库列的可空性：
#   shopping_lists  只有 note 可空
#   shopping_items  只有 note 可空
# category_id 看着像可空的，其实不是 —— 数据库是 NOT NULL DEFAULT 0，
# 「清空分类」的正确表达是 0，不是 null（和 tasks 的 CategoryId 同一个约定）。
NULLABLE_FIELDS = ("note",)

DEFAULT_UNIT = "个"


# ---------------------------------------------------------------- 通用清洗


def _clean_required(value: str, label: str) -> str:
    """先 strip 再判空 —— 光有 min_length 拦不住 '   '（3 个空格长度是 3）。"""
    text = value.strip()
    if not text:
        raise ValueError(f"{label}不能为空")
    return text


def _clean_note(value: str | None) -> str | None:
    """备注：全空白等同没填，存 None 而不是空串。

    空串和 None 都对，但如果同一次请求里一会儿存 ""、一会儿存 null，
    前端就要写两套判断。统一成 None。
    """
    if value is None:
        return None
    text = value.strip()
    return text or None


def _clean_unit(value: str) -> str:
    text = value.strip()
    return text or DEFAULT_UNIT


# ---------------------------------------------------------------- 清单族


class ListCreate(BaseModel):
    """新建清单的请求体。

    没有 status：新建出来一律 active。归档是「之后」的动作，走 PATCH。
    没有 user_id：见模块 docstring。
    """

    name: str = Field(min_length=1, max_length=64, description="清单名，如「周末采购」")
    note: str | None = Field(default=None, max_length=255, description="备注")

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        return _clean_required(value, "清单名")

    @field_validator("note")
    @classmethod
    def note_clean(cls, value: str | None) -> str | None:
        return _clean_note(value)


class ListUpdate(BaseModel):
    """改清单（PATCH）—— 三个字段全可选，三态语义：

      没传该字段     保持原值   ← 靠 model_dump(exclude_unset=True) 区分
      传 null        清空       ← 只有 note 允许
      传了值         改成新值

    `name` 和 `note` 分开校验的理由不一样：
      name 传 null  → 422（数据库 NOT NULL，而且一张没有名字的清单没法显示）
      note 传 null  → 清空
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=255)
    status: ListStatus | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str | None) -> str | None:
        # None 交给下面的 model_validator 拦，这里只处理「传了字符串」
        return value if value is None else _clean_required(value, "清单名")

    @field_validator("note")
    @classmethod
    def note_clean(cls, value: str | None) -> str | None:
        return _clean_note(value)

    @model_validator(mode="after")
    def check_patch_fields(self) -> "ListUpdate":
        """两条拦在进 crud 之前的规则。"""
        if not self.model_fields_set:
            raise ValueError("没有需要修改的字段")

        # 除 note 外的列都是 NOT NULL。与其让 SQL 抛 IntegrityError（→500），
        # 不如在这里给明确的 422。
        for field_name in self.model_fields_set - set(NULLABLE_FIELDS):
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 不能为 null")
        return self


class ListOut(BaseModel):
    """一张清单的对外形状。

    两个计数字段是**服务端算的**，不是前端自己数：
      item_count        这项清单里一共多少条明细
      unpurchased_count 还没买的条数 —— 就是左栏那个角标

    为什么不让前端拿到明细再自己数：清单列表页不加载任何明细，
    10 张清单就意味着 1 + 10 次请求，而且每加一条物品都要把整页重算一遍。
    一个 LEFT JOIN 能算完的事，放在接口里一次给出。
    """

    id: int
    name: str
    note: str | None = None
    status: ListStatus
    item_count: int = Field(default=0, description="明细总条数")
    unpurchased_count: int = Field(default=0, description="还没买的条数")
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(
        cls,
        shopping_list,
        *,
        item_count: int = 0,
        unpurchased_count: int = 0,
    ) -> "ListOut":
        """ORM 对象 → 响应体。

        两个计数由调用方传入（crud 层用一条聚合查询算好），
        不在这里再查库 —— 响应模型不该有 IO。
        """
        return cls(
            id=shopping_list.id,
            name=shopping_list.name,
            note=shopping_list.note,
            status=shopping_list.status,
            item_count=item_count,
            unpurchased_count=unpurchased_count,
            created_at=to_beijing_iso(shopping_list.created_at),
            updated_at=to_beijing_iso(shopping_list.updated_at),
        )


# ---------------------------------------------------------------- 明细族


class ItemCreate(BaseModel):
    """往清单里加一条的请求体。

    `quantity` 用 Decimal 接，不接受字符串形式的负数、也不接受 "abc"：
    Pydantic 会自己给 422，报错信息里带字段名。

    `source` 不在这里：页面加的一律 manual。AI 加的那条走
    execute.py 直接写库，不经这个模型（见 V1.4 第 3 步），
    否则「机器人和用户」的区别就变成了客户端一句话的事。
    """

    name: str = Field(min_length=1, max_length=120, description="物品名，如「牛奶」")
    quantity: Decimal = Field(
        default=Decimal("1"),
        gt=Decimal("0"),
        le=MAX_QUANTITY,
        max_digits=10,
        decimal_places=2,
        description="数量，必须 > 0；最多两位小数",
    )
    unit: str = Field(default=DEFAULT_UNIT, max_length=16, description="单位，如 个/箱/斤")
    category_id: int = Field(
        default=0, ge=0, description="0 = 未分类；非 0 时必须是 scope=shopping 的分类"
    )
    note: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        return _clean_required(value, "物品名")

    @field_validator("unit")
    @classmethod
    def unit_not_blank(cls, value: str) -> str:
        return _clean_unit(value)

    @field_validator("note")
    @classmethod
    def note_clean(cls, value: str | None) -> str | None:
        return _clean_note(value)


class ItemUpdate(BaseModel):
    """改一条明细（PATCH）。

    **故意不含 is_purchased**：勾选是独立接口（PATCH .../purchased），
    因为它要联动 purchased_at。同一个字段在通用 PATCH 里「顺手能改」
    的话，用户改个名字时传了 is_purchased=1，purchased_at 就会是空的 ——
    数据能救，但「今天买了什么」的统计就悄悄错了。
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    quantity: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        le=MAX_QUANTITY,
        max_digits=10,
        decimal_places=2,
    )
    unit: str | None = Field(default=None, max_length=16)
    category_id: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str | None) -> str | None:
        return value if value is None else _clean_required(value, "物品名")

    @field_validator("unit")
    @classmethod
    def unit_not_blank(cls, value: str | None) -> str | None:
        return value if value is None else _clean_unit(value)

    @field_validator("note")
    @classmethod
    def note_clean(cls, value: str | None) -> str | None:
        return _clean_note(value)

    @model_validator(mode="after")
    def check_patch_fields(self) -> "ItemUpdate":
        if not self.model_fields_set:
            raise ValueError("没有需要修改的字段")

        for field_name in self.model_fields_set - set(NULLABLE_FIELDS):
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 不能为 null")
        return self


class ItemPurchased(BaseModel):
    """勾选 / 取消勾选 —— 只有一个必填字段。

    为什么不复用 ItemUpdate：is_purchased 在这里必须「必填」，
    在 ItemUpdate 里却「不许出现」。同一个字段两种约束，
    拆成两个模型比在 crud 里加 if 清楚（和待办的 TaskStatusUpdate 同理）。

    用 bool 而不是 0/1：JSON 里 true/false 不会被误读成「没传」，
    而 0 在某些前端序列化里会变成 null。
    """

    is_purchased: bool


class ItemOut(BaseModel):
    """一条明细的对外形状。

    quantity 是字符串：Decimal("2.50") → "2.50"。
    前端按右对齐显示即可，不需要知道它是从 DECIMAL 来的。

    category_name 和待办一样由调用方传 {id: 名字} 查出来，
    不在这里查库。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    list_id: int
    name: str
    quantity: str
    unit: str
    category_id: int
    category_name: str | None = None
    is_purchased: bool
    purchased_at: str | None
    note: str | None = None
    sort_order: int
    source: ItemSource
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(cls, item, category_names: dict[int, str] | None = None) -> "ItemOut":
        names = category_names or {}
        return cls(
            id=item.id,
            list_id=item.list_id,
            name=item.name,
            # Decimal("2.50") → "2.50"；小数位数由 DECIMAL(10,2) 保证
            quantity=str(item.quantity),
            unit=item.unit,
            category_id=item.category_id,
            category_name=names.get(item.category_id),
            # 库里是 TINYINT 0/1，对外一律 bool
            is_purchased=bool(item.is_purchased),
            purchased_at=to_beijing_iso(item.purchased_at),
            note=item.note,
            sort_order=item.sort_order,
            source=item.source,
            created_at=to_beijing_iso(item.created_at),
            updated_at=to_beijing_iso(item.updated_at),
        )


class ListDetailOut(BaseModel):
    """`GET /shopping-lists/{id}` 的返回：清单本身 + 全部明细。

    为什么不平铺成一个对象：明细是列表、清单是单条，
    包一层之后前端不用去分辨「哪个字段是列表还是单条」
    （知识库的详情接口也是这个形状，两处一致）。
    """

    list: ListOut
    items: list[ItemOut]