"""FastAPI 公共依赖。

V1.0：没有登录系统，所有请求视为固定演示用户（id=1）。
V1.2：把 `get_current_user_id` 的实现换成「从 JWT 解析」，
      **所有业务接口不需要改一行代码**——这就是把认证收敛成单一依赖的收益。
"""

from app.core.errors import unauthorized

DEMO_USER_ID = 1


def get_current_user_id() -> int:
    """返回当前用户 ID。

    V1.0 固定返回演示用户；V1.2 会替换为 Token 解析（见 docs/工程规范.md 的认证抽象约定）。
    注意：user_id 永远由后端注入，**不接受客户端传参**。
    """
    return DEMO_USER_ID


def require_admin(current_user_id: int) -> None:
    """V1.2 起使用：非管理员抛 403。当前仅为占位，避免接口层提前写死判断。"""
    raise unauthorized("V1.0 尚未启用角色权限")
