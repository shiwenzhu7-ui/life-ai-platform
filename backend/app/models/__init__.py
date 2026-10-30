"""模型汇总：Alembic autogenerate 依赖本模块的 import 来发现所有表。

新增模型时必须在这里导出，否则 `alembic revision --autogenerate` 看不到新表。
"""

from app.models.category import Category
from app.models.expense import Expense
from app.models.task import TASK_PRIORITIES, TASK_STATUSES, Task
from app.models.user import User

__all__ = [
    "User",
    "Category",
    "Task",
    "Expense",
    "TASK_STATUSES",
    "TASK_PRIORITIES",
]
