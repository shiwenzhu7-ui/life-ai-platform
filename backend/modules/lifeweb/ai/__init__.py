"""AI 助手模块（V1.1）。

四张表，各管一段：

    chat_sessions      一次对话（左侧列表的一行）
    chat_messages      对话里的一条消息（用户 / 助手 / 系统 / 工具）
    ai_pending_actions AI 想帮你写库，但**等你点确认**的草稿
    ai_usage_logs      每次调模型的计费流水（配额判断靠它）

本文件只做「把模型类挂上来」，让 `import modules.lifeweb.ai` 就能拿到全部四张表；
真正的接口（路由 / 服务）在后续步骤里加。
"""

from modules.lifeweb.ai.chat_message import ChatMessage
from modules.lifeweb.ai.chat_session import ChatSession
from modules.lifeweb.ai.pending_action import PendingAction
from modules.lifeweb.ai.usage_log import UsageLog

__all__ = ["ChatMessage", "ChatSession", "PendingAction", "UsageLog"]
