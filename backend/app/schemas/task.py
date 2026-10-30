"""待办事项的请求/响应 Schema。

校验规则对应 docs/接口设计.md §4。
时间约定：请求可带 +08:00 偏移；响应统一输出带 +08:00 的 ISO8601。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.time import now_utc, to_local

Priority = Literal["low", "medium", "high"]
Status = Literal["todo", "doing", "done", "cancelled"]


class TaskCreate(BaseModel):
    """新建待办。"""

    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    category_id: int = Field(default=0, ge=0)
    priority: Priority = "medium"
    due_at: datetime | None = None
    remind_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("标题不能为空")
        return value

    @field_validator("description")
    @classmethod
    def description_strip(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def remind_before_due(self) -> "TaskCreate":
        if self.due_at and self.remind_at and self.remind_at >= self.due_at:
            raise ValueError("提醒时间必须早于截止时间")
        return self


class TaskUpdate(BaseModel):
    """局部更新：只传需要改的字段。

    category_id / priority 传 None 表示「不改」，因此这里不加 default 以外的语义。
    """

    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    category_id: int | None = Field(default=None, ge=0)
    priority: Priority | None = None
    due_at: datetime | None = None
    remind_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("标题不能为空")
        return value

    @model_validator(mode="after")
    def remind_before_due(self) -> "TaskUpdate":
        if self.due_at and self.remind_at and self.remind_at >= self.due_at:
            raise ValueError("提醒时间必须早于截止时间")
        return self


class TaskStatusUpdate(BaseModel):
    """状态切换。"""

    status: Status


class TaskOut(BaseModel):
    """待办响应体。"""

    id: int
    title: str
    description: str | None
    category_id: int
    category_name: str | None = None
    priority: str
    status: str
    due_at: datetime | None
    remind_at: datetime | None
    completed_at: datetime | None
    is_overdue: bool
    source: str
    created_at: datetime

    @classmethod
    def from_model(cls, task, category_name: str | None = None) -> "TaskOut":
        """把 ORM 对象转成响应体。

        - 所有 naive UTC 时间在这里统一转成带 +08:00 的 aware datetime
        - is_overdue 由当前时间与状态实时算出，不落库（避免数据陈旧）
        """
        return cls(
            id=task.id,
            title=task.title,
            description=task.description,
            category_id=task.category_id,
            category_name=category_name,
            priority=task.priority,
            status=task.status,
            due_at=to_local(task.due_at),
            remind_at=to_local(task.remind_at),
            completed_at=to_local(task.completed_at),
            is_overdue=task.is_overdue(now_utc()),
            source=task.source,
            created_at=to_local(task.created_at) or now_utc(),
        )
