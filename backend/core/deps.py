"""FastAPI 公共依赖 —— 「当前用户是谁」的唯一入口。

V1.0：没有登录系统，所有请求都视为固定演示用户（id=1）。
V1.2：把 get_current_user_id 换成「解 JWT」。
V2.0：再换成「Keycloak JWKS 验签 + sub 关联本地用户」。

三次升级都只改这一个函数的实现，业务接口一行都不用动。
"""

DEMO_USER_ID = 1


def get_current_user_id() -> int:
    """当前用户 ID。

    注意：user_id 永远由后端注入，**不接受客户端传参**。
    否则任何人都能在请求体里塞一个别人的 user_id 往别人账户写数据。
    """
    return DEMO_USER_ID
