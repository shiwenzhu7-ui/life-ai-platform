"""AI 助手模块（V1.1）。

四张表，各管一段：

    chat_sessions      一次对话（左侧列表的一行）
    chat_messages      对话里的一条消息（用户 / 助手 / 系统 / 工具）
    ai_pending_actions AI 想帮你写库，但**等你点确认**的草稿
    ai_usage_logs      每次调模型的计费流水（配额判断靠它）

本文件只做「把模型类挂上来」，让 `import modules.lifeweb.ai` 就能拿到全部四张表；
接口在 `router.py`（A3：会话 CRUD；A4：`POST /ai/chat` 发消息），
编排在 `service.py`（A4），提示词在 `prompts.py`，模型调用在 `provider.py`（A2）。
"""

from modules.lifeweb.ai.chat_message import ChatMessage
from modules.lifeweb.ai.chat_session import ChatSession
from modules.lifeweb.ai.pending_action import PendingAction
from modules.lifeweb.ai.usage_log import UsageLog

__all__ = ["ChatMessage", "ChatSession", "PendingAction", "UsageLog"]
