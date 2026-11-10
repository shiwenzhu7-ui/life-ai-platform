"""FastAPI 公共依赖 —— 「当前用户是谁」的唯一入口。

V1.0：没有登录系统，所有请求都视为固定演示用户（id=1）。
V1.2：把 get_current_user_id 换成「解 JWT」。
V2.0：再换成「Keycloak JWKS 验签 + sub 关联本地用户」。

三次升级都只改这一个文件的实现，业务接口一行都不用动 ——
41 处路由写的都是 `Depends(get_current_user_id)`，函数名和签名不变。

三个依赖的分工（别混用）：

    get_current_user_id   只验签，不查库。用于**所有业务接口**。
                          开销最小；代价是被禁用用户在令牌过期前仍能访问。
    get_current_user      验签 + 查库，拿到 role / status / 额度。
                          /auth/me、管理接口、任何「要看角色」的地方用它。
    require_role(...)     在 get_current_user 基础上判角色，不够就 403。

为什么不把「禁用检查」放进 get_current_user_id：
    那等于每个业务请求多一次 SELECT。禁用用户的实际损失是「最多多用到令牌过期
    （2 小时）」，而所有请求多一次查库是永久的常数开销。等真有需要再换。
"""

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import forbidden, unauthorized, user_disabled
from core.security import decode_token
from modules.shared.users.model import User

# auto_error=False：没带 Authorization 头时返回 None 而不是直接抛 403。
# FastAPI 默认（auto_error=True）抛的是 403，可需求要求「未登录 → 401」，
# 所以这里关掉自动报错，自己按 401 处理。
_bearer = HTTPBearer(auto_error=False, description="登录接口返回的 access_token")


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> int:
    """当前用户 ID —— 只验签，不查库。

    注意：user_id 永远由后端从令牌里取，**不接受客户端传参**。
    否则任何人都能在请求体里塞一个别人的 user_id 往别人账户写数据。
    """
    if credentials is None or not credentials.credentials.strip():
        raise unauthorized("请先登录")
    # 令牌里的 sub 是不是真用户、账号是否被禁用，这里不管 ——
    # decode_token 只保证「签名对、没过期」，身份有效性由 get_current_user 负责。
    return decode_token(credentials.credentials.strip())


def get_current_user(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> User:
    """当前用户完整记录（会查库）。角色、状态、额度都从这里读。"""
    user = db.get(User, user_id)
    # 令牌有效但人已删：等同于登录失效，让他重新登录，而不是报「无权限」
    if user is None or user.deleted_at is not None:
        raise unauthorized("登录已失效，请重新登录")
    if user.status != "active":
        raise user_disabled()
    return user


def require_role(*roles: str):
    """角色门禁工厂：`Depends(require_role("admin"))`。

    多角色取「满足其一」。admin 不自动放行 vip —— 显式写出来更好查。
    """

    def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise forbidden("无权访问")
        return user

    return _checker