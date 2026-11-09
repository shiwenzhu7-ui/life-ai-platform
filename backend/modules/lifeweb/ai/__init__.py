"""AI 助手模块（V1.1）。

目录分工：

    models/     四张表（会话 / 消息 / 待确认写入 / 用量），见 `models/__init__.py`
    schema.py   请求 / 响应字段
    crud.py     数据访问（含 A4 的 add_message / touch_session / add_usage_log）
    service.py  编排：发消息 → 调模型 → 落库（A4，A11 接前情摘要）
    summary.py  长对话的前情摘要：滚动压缩 + 增量更新，失败不影响对话（A11）
    prompts.py  系统提示词（A5 第 4 步加回工具规则）
    provider.py 模型调用（A2）
    tools.py    工具声明（发给模型的说明书，A5 第 1 步写入类 / A6 只读类 / A7 update_task / A8 create_expense / A9 query_expenses / A10 delete_task）
    action_schema.py 工具参数校验 + 草稿解析（A5 第 2 步 / A6 只读参数 / A7 UpdateTaskPayload / A8 CreateExpensePayload / A9 QueryExpensesPayload / A10 DeleteTaskPayload）
    pending.py  草稿落库 / 预览 / 过期与状态机（A5 第 3 步，A7 加 before，A8 加 expense 域与金额文案，A10 加 gone 快照）
    tool_flow.py 一次问答里「工具这一支」的编排（A5 第 4 步，A6 改成最多 3 轮循环，A9 加 query_expenses 分支）
    execute.py  用户确认卡片后真正写业务表（A5 第 5 步，A7 加 _write_update，A8 加 _write_expense，A10 加 _write_delete）
    router.py   接口：会话 CRUD（A3）+ `POST /ai/chat`（A4/A5/A6）+ 确认 / 取消（A5）

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
