"""模型汇总 —— 全项目唯一需要「把每个 model.py 都 import 一遍」的地方。

为什么必须有它：
    SQLAlchemy 是靠「模型类被 import 过」来收集表的。
    如果没有任何代码 import 某个 model.py，Base.metadata 里就不会有那张表，
    后果是 Alembic autogenerate 看不到表、create_all 也不建它。

规矩：
    新增一个模块、并且它有自己的表 → 在下面加一行 import。
    只读聚合的模块（dashboard / analytics）没有 model.py，不用加。

app.py 会 import 本模块，所以服务一启动元数据就是完整的。
"""

from modules.lifeweb.ai.chat_message import ChatMessage  # noqa: F401
from modules.lifeweb.ai.chat_session import ChatSession  # noqa: F401
from modules.lifeweb.ai.pending_action import PendingAction  # noqa: F401
from modules.lifeweb.ai.usage_log import UsageLog  # noqa: F401
from modules.lifeweb.expenses.model import Expense  # noqa: F401
from modules.lifeweb.shopping.model import ShoppingItem, ShoppingList  # noqa: F401
from modules.lifeweb.tasks.model import Task  # noqa: F401
from modules.shared.categories.model import Category  # noqa: F401
from modules.shared.users.model import User  # noqa: F401

__all__ = [
    "Category",
    "ChatMessage",
    "ChatSession",
    "Expense",
    "PendingAction",
    "ShoppingItem",
    "ShoppingList",
    "Task",
    "UsageLog",
    "User",
]
