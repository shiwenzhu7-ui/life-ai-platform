"""AI 助手模块（V1.1）。

目录分工：

    models/     四张表（会话 / 消息 / 待确认写入 / 用量），见 `models/__init__.py`
    schema.py   请求 / 响应字段
    crud.py     数据访问（含 A4 的 add_message / touch_session / add_usage_log）
    service.py  编排：发消息 → 调模型 → 落库（A4）
    prompts.py  系统提示词（A5 第 4 步加回工具规则）
    provider.py 模型调用（A2）
    tools.py    工具声明（发给模型的说明书，A5 第 1 步）
    action_schema.py 工具参数校验 + 草稿解析（A5 第 2 步）
    pending.py  草稿落库 / 预览 / 过期与状态机（A5 第 3 步）
    tool_flow.py 一次问答里「工具这一支」的编排 + 坏参数重试（A5 第 4 步）
    execute.py  用户确认卡片后真正写业务表（A5 第 5 步）
    router.py   接口：会话 CRUD（A3）+ `POST /ai/chat`（A4/A5）+ 确认 / 取消（A5）

本文件只做 re-export，让 `import modules.lifeweb.ai` 就能拿到全部四张表的类；
表定义本身在 `models/` 子包里，一个表一个文件。
"""

from modules.lifeweb.ai.models import (
    ChatMessage,
    ChatSession,
    PendingAction,
    UsageLog,
)

__all__ = ["ChatMessage", "ChatSession", "PendingAction", "UsageLog"]
