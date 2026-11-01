"""系统管理后台 `admin-web`（端口 8082）。

注意包名是 `adminweb` 不是 `admin-web`：Python 包名不允许连字符
（`import admin-web` 是语法错误）。

本端所有接口都以 `/admin` 开头，并且要求 role=admin。
权限靠后端的 require_role("admin") 拦，前端路由守卫只是体验。

V1.2 计划放的模块：users（用户管理）/ roles（角色说明）
V1.4 再补：logs / stats / ai_configs / public_documents
"""

from fastapi import APIRouter

router = APIRouter()

# ---------- 用户管理（V1.2） ----------
# from modules.adminweb.users.router import router as admin_users_router
# router.include_router(admin_users_router)
