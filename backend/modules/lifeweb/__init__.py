"""生活管理端 `frontend`（端口 8080）。

本端所有路由在这里汇总成一个 router，app.py 只挂这一个 ——
所以加新模块**只改这个文件**，永远不用动 app.py。

加一个功能模块的三步：
    1. 建模块文件夹：modules/lifeweb/tasks/{model,schema,crud,router}.py
    2. 在下面加两行（import + include_router）
    3. 完事

V1.0 计划放的模块：tasks / expenses / dashboard
V1.4 再补：budgets / shopping / life / diary
V1.1：ai      V1.3：documents
"""

from fastapi import APIRouter

from modules.lifeweb.dashboard.router import router as dashboard_router
from modules.lifeweb.expenses.router import router as expenses_router
from modules.lifeweb.tasks.router import router as tasks_router
from modules.shared.categories.router import router as categories_router

router = APIRouter()

# ---------- 分类字典（shared，待办/消费/购物页共用下拉） ----------
router.include_router(categories_router)

# ---------- 待办（V1.0） ----------
router.include_router(tasks_router)

# ---------- 消费（V1.0） ----------
router.include_router(expenses_router)

# ---------- 首页（V1.0，只读聚合 tasks + expenses） ----------
router.include_router(dashboard_router)
