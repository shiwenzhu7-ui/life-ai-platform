"""数据分析端 `analytics-web`（端口 8081）。

本端是**纯只读聚合**：不产生任何数据，也不写库。
所以它的模块通常**没有 model.py**，要读别人的表就 import 那个模块的 model：

    from modules.lifeweb.tasks.model import Task

这行 import 就是"分析端读了待办表"的书面证据，别用复制模型的方式绕开它。

V1.4 计划放的模块：analytics（overview / expense / budget / task / life / report）
"""

from fastapi import APIRouter

router = APIRouter()

# ---------- 分析总览与各维度（V1.4） ----------
# from modules.analyticsweb.analytics.router import router as analytics_router
# router.include_router(analytics_router)
