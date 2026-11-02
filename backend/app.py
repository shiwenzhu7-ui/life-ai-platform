"""LifeAI 后端入口 —— 唯一的 FastAPI 实例。

这里只做四件事：建 app、挂中间件、注册异常处理、把三端的路由挂上去。
业务代码一律不放这里。

启动（在 backend/ 目录下）：
    .\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000
文档：
    http://127.0.0.1:8000/docs
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

import modules.registry  # noqa: F401 —— 导入即注册全部表，别删
from core.config import settings
from core.db import engine
from core.errors import register_exception_handlers
from modules.adminweb import router as adminweb_router
from modules.analyticsweb import router as analyticsweb_router
from modules.lifeweb import router as lifeweb_router

app = FastAPI(title=settings.app_name, version="0.1.0")

# 三个前端分别跑在 8080 / 8081 / 8082（见 docs/工程规范.md §6.3）。
# 不要写 allow_origins=["*"] 再配 allow_credentials=True：那等于对任意网站开放带凭证的跨域。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

# ---------- 三端路由 ----------
# 每一端的 router 在它自己的 __init__.py 里汇总本端所有模块，
# 所以这里永远只有三行，加新模块不用动这个文件。
app.include_router(lifeweb_router, prefix=settings.api_v1_prefix)  # 生活管理端
app.include_router(analyticsweb_router, prefix=settings.api_v1_prefix)  # 数据分析端
app.include_router(adminweb_router, prefix=settings.api_v1_prefix)  # 系统管理后台


# ---------- 运维探针（不带 /api/v1 前缀） ----------


@app.get("/healthz", tags=["健康检查"], summary="进程存活检查")
def healthz() -> dict:
    """活着就返回 ok，用来确认服务和端口是通的。"""
    return {"status": "ok"}


@app.get("/readyz", tags=["健康检查"], summary="依赖就绪检查（含 MySQL）")
def readyz() -> dict:
    """真正连一次数据库。这个方法能过，说明 core/db.py 的配置是对的。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 —— 就绪检查不能把异常抛给运维探针
        return {"status": "not_ready", "database": "error"}
    return {"status": "ready", "database": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=settings.port, reload=True)
