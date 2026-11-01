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

from modules.lifeweb.tasks.router import router as tasks_router

router = APIRouter()

# ---------- 待办（V1.0） ----------
router.include_router(tasks_router)

# ---------- 消费（V1.0） ----------
# ---------- 首页（V1.0） ----------
