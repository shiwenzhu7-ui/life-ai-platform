"""用户管理（V1.4）：`/admin/users` 和 `/admin/usage`。

为什么 usage 也放在 `users/` 下：它按用户筛选、按用户展示，
数据来源是 `ai_usage_logs`，但它回答的是「这些人在用 AI 花多少钱」，
跟用户管理是同一件事的两面。等 V1.4 后段真做 `/settings`（模型配置）时
再另开 `configs/` 子模块。
"""

from modules.adminweb.users.router import router

__all__ = ["router"]