"""认证模块（V1.2，三端共用）。

表：`users`（`modules/shared/users/model.py`；V1.2 由迁移 `b1users` 补全列）。
为什么放 shared：admin-web 和 analytics-web 也要登录，不属于任何一端。

接口见 router.py；业务逻辑见 service.py；口令哈希与 JWT 在 `core/security.py`，
「当前用户是谁」在 `core/deps.py` —— 本模块只是把它们串成 HTTP。

V2.0 接 Keycloak 时：service.login 改成「校验 SSO 回调」，router 形状不变。
"""

from fastapi import APIRouter

from modules.shared.auth.router import router as auth_router

router = APIRouter()
router.include_router(auth_router)

__all__ = ["router"]