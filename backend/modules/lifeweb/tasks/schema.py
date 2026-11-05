"""待办模块的请求/响应字段。

出参一律在这里转换（时间转北京时间、is_overdue 现算），
不让 router 或 crud 各写一遍。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from core.time import to_beijing_iso

Priority = Literal["low", "medium", "high"]

# 必须与 model.TASK_STATUSES 一字不差（模型里那份是数据库 ENUM 的镜像）
TaskStatus = Literal["todo", "doing", "done", "cancelled"]

# 列表排序：三个键 + 升降序（docs/页面设计.md:81 的元素表要求能按优先级/创建时间排）
TaskSort = Literal["due_at", "created_at", "priority"]
SortOrder = Literal["asc", "desc"]

# 允许被显式改成 null 的字段（其余字段传 null 一律 422）
NULLABLE_FIELDS = ("description", "due_at")


def _clean_title(value: str) -> str:
    """先 strip 再判空 —— 光有 min_length 拦不住 '   '（3 个空格长度是 3）。"""
    value = value.strip()
    if not value:
        raise ValueError("标题不能为空")
    return value


class TaskCreate(BaseModel):
    """新建待办的请求体 —— 只列「前端允许传」的字段。

    这里没有 user_id / status / created_at：
      user_id    由服务端从依赖注入决定（否则能往别人账户塞数据）
      status     新建的一律是 todo
      created_at 由数据库填
    """

    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    # 0 = 未分类。不是 Optional：数据库里是 NOT NULL DEFAULT 0，
    # 「没有分类」用 0 表达，比 null 少一种状态。
    category_id: int = Field(default=0, ge=0)
    priority: Priority = "medium"
    due_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        return _clean_title(value)


class TaskUpdate(BaseModel):
    """局部更新（PATCH）—— 四个字段全是可选的，三态语义：

      没传该字段     保持原值   ← 靠 model_dump(exclude_unset=True) 区分
      传 null        清空       ← 只有 description / due_at 允许
      传了值         改成新值

    这里没有 status：改状态是独立接口（PATCH /tasks/{id}/status），
    因为它要联动 completed_at，规则跟改文案不是一回事。
    """

    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    # 传 0 = 取消分类；传 null 会 422（数据库列 NOT NULL），
    # 所以它不在 NULLABLE_FIELDS 里 —— 「清空分类」的正确表达是 0，不是 null。
    category_id: int | None = Field(default=None, ge=0)
    priority: Priority | None = None
    due_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str | None) -> str | None:
        # None 会在下面的 model_validator 里被拦；这里只处理「传了字符串」
        return value if value is None else _clean_title(value)

    @model_validator(mode="after")
    def check_patch_fields(self) -> "TaskUpdate":
        """两条拦在进 crud 之前的规则。"""
        if not self.model_fields_set:
            raise ValueError("没有需要修改的字段")

        # 数据库里除 description / due_at 外都是 NOT NULL，
        # 与其让 SQL 抛 IntegrityError（→500），不如在这里给明确的 422。
        for name in self.model_fields_set - set(NULLABLE_FIELDS):
            if getattr(self, name) is None:
                raise ValueError(f"{name} 不能为 null")
        return self


class TaskStatusUpdate(BaseModel):
    """切状态（勾选框专用）—— 只有一个必填字段。

    为什么单独一个模型而不是复用 TaskUpdate：
      status 在这里必须是「必填」，在 TaskUpdate 里却是「不许出现」。
      同一个字段两种约束，拆成两个模型比加一堆 if 清楚。
    """

    status: TaskStatus


class TaskOut(BaseModel):
    id: int
    title: str
    description: str | None
    category_id: int = Field(description="0=未分类")
    category_name: str | None = Field(default=None, description="分类名；未分类时为 null")
    status: str
    priority: str
    due_at: str | None
    completed_at: str | None
    created_at: str | None
    is_overdue: bool

    @classmethod
    def from_model(
        cls,
        task,
        now_utc: datetime,
        category_names: dict[int, str] | None = None,
    ) -> "TaskOut":
        """ORM 对象 → 响应体。

        三个转换在这里一次做完：
          naive UTC → 带 +08:00 的 ISO 字符串
          is_overdue 按当前时间现算（不落库）
          category_id → category_name（由调用方传入的 {id: 名字} 查表得到）
        """
        names = category_names or {}
        return cls(
            id=task.id,
            title=task.title,
            description=task.description,
            category_id=task.category_id,
            category_name=names.get(task.category_id),
            status=task.status,
            priority=task.priority,
            due_at=to_beijing_iso(task.due_at),
            completed_at=to_beijing_iso(task.completed_at),
            created_at=to_beijing_iso(task.created_at),
            is_overdue=task.is_overdue(now_utc),
        )
