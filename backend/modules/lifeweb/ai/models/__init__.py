"""AI 相关的四张表 —— 一个表一个文件，统一从这个包 import。

    chat_sessions       一次对话（左侧列表的一行）
    chat_messages       对话里的一条消息（user / assistant / system / tool）
    ai_pending_actions  AI 想帮你写库、但**等你点确认**的草稿（A5 用）
    ai_usage_logs       每次调模型的计费流水（配额判断靠它）

为什么单独一个子包：表定义会跟着版本长（A1 四张，后面还有 V1.2 的配额、V1.3 的知识库），
和 schema / crud / router / service 混在同一层时，`ai/` 目录里一半是表、一半是接口。
分出去以后，业务代码只认这一行：

    from modules.lifeweb.ai.models import ChatSession

内部怎么拆文件（一个表一个），外面不用知道。
"""

from modules.lifeweb.ai.models.chat_message import ChatMessage
from modules.lifeweb.ai.models.chat_session import ChatSession
from modules.lifeweb.ai.models.pending_action import PendingAction
from modules.lifeweb.ai.models.usage_log import UsageLog

__all__ = ["ChatMessage", "ChatSession", "PendingAction", "UsageLog"]
