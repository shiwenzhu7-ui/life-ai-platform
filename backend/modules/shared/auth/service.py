"""认证业务逻辑（V1.2）。

这里只做「决定」：这个人能不能注册、这对账号口令对不对。
HTTP 状态码由 router 翻译 —— service 抛 AppError，router 不写 if。

两条安全约定：
    1. 登录失败永远是同一句话，不区分「用户不存在」「密码错」「口令为空」。
       区分了就等于把「哪些用户名存在」免费送给攻击者。
    2. 禁用判断放在**密码校验之后**。反过来的话，不知道密码的人也能探测出
       哪些账号被禁用了。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import settings
from core.errors import conflict, unauthorized, validation_error
from core.security import create_access_token, hash_password, verify_password
from core.time import now_utc
from modules.adminweb.users.permission import is_root
from modules.shared.auth.schema import LoginIn, RegisterIn, TokenOut, UserBrief, UserOut
from modules.shared.users.model import User

# 登录失败统一文案
_BAD_CREDENTIALS = "用户名或密码错误"


def register(db: Session, data: RegisterIn) -> User:
    """注册。成功返回新用户（未 commit 由调用方决定？不 —— 这里自己 commit）。"""
    if db.scalar(select(User.id).where(User.username == data.username)) is not None:
        raise conflict("用户名已被占用")
    if data.email and db.scalar(select(User.id).where(User.email == data.email)) is not None:
        raise conflict("邮箱已被注册")

    try:
        password_hash = hash_password(data.password)
    except ValueError as exc:
        # schema 已经卡过 72 字节，这里是最后一道兜底（比如走的是别的入口）
        raise validation_error(str(exc)) from None

    user = User(
        username=data.username,
        email=data.email,
        password_hash=password_hash,
        role="user",  # 写死：绝不让请求体决定角色
        status="active",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def login(db: Session, data: LoginIn) -> TokenOut:
    """登录。任何一步不对都 401，只有「密码对了但账号禁用」才 403。"""
    # V1.3 待办：此处应加失败次数限流（按 username + IP），防止暴力破解。
    # V1.2 没有这条要求，先不做，也不假装做了。
    user = db.scalar(select(User).where(User.username == data.username.strip()))

    if user is None or user.deleted_at is not None:
        raise unauthorized(_BAD_CREDENTIALS)
    # 用户存在但没有口令（V2.0 起由 Keycloak 管的账号）：同样按凭据错误处理
    if not verify_password(data.password, user.password_hash):
        raise unauthorized(_BAD_CREDENTIALS)

    # 到这里密码是对的，才允许暴露「账号被禁用」这个事实
    if user.status != "active":
        from core.errors import user_disabled

        raise user_disabled()

    user.last_login_at = now_utc()
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id)
    return TokenOut(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        user=UserBrief(
            id=user.id,
            username=user.username,
            nickname=user.nickname,
            role=user.role,
            # 登录响应必须自带 is_root_admin，不能只靠随后的 /auth/me 补 ——
            # 前端拿登录响应直接 SET_USER，缺这个字段时根管理员会被当成普通管理员
            is_root_admin=is_root(db, user),
        ),
    )


def me(db: Session, user: User) -> UserOut:
    """当前用户信息。role/status 由依赖层校验过（get_current_user）。

    V1.2 曾在这里顺带返回 AI 每日额度与今日已用次数（要查一次 ai_usage_logs）。
    2026-11-10 取消额度限制后，这里不再碰用量数据。

    V1.4 为了返回 `is_root_admin` 又碰了一次库：根管理员可能是「id 最小的 admin」
    （随库变化），不能只凭 JWT 里的 role 判断。只走一次按 id 排序的 limit 1 查询。
    """
    return UserOut(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
        role=user.role,
        is_root_admin=is_root(db, user),
    )