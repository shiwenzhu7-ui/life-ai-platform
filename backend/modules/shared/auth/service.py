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


def sso_sync(db: Session, *, sub: str, username: str | None, email: str | None) -> User:
    """把「Keycloak 说你是谁」落到本地 users 表的一行（V2.0 SSO）。

    三种情况，按这个顺序判断：
        1. keycloak_sub 已经绑过   → 就是这个用户，更新邮箱/昵称不急，先不动
        2. 有同名本地账号          → 认领：把 sub 绑上去，auth_source 改 keycloak
        3. 都没有                  → 新建一个 role=user 的普通用户

    为什么用 sub 而不是邮箱/用户名认人：
        邮箱可以在 Keycloak 里改（甚至被管理员改给别人），用户名也可能被回收；
        sub 是这个 realm 里永不变的 UUID。用它绑定，「同一个 Keycloak 账号」
        和「同一行本地用户」才是一一对应的。

    为什么第 2 步允许「同名认领」：
        老用户 demo 在本地已经有数据（待办、账本、知识库）。他第一次走 SSO 时，
        如果不认领，会凭空多出一个新账号，之前的数据全看不到。
        代价是「有人在 Keycloak 里注册了和你同名的账号」就能接管 —— 所以：
            · 只认**没有绑过 sub** 的本地账号（已绑定的一律不抢）
            · 认领后立刻写 auth_source=keycloak，管理后台能看出来源变了

    角色永远不由这里决定：新账号一律 role=user。Keycloak 里就算有人给自己
    加了 admin 角色也不会传过来 —— 角色归本地 users.role（决策见 docs/项目规划.md）。
    """
    if not sub:
        raise unauthorized("登录校验未通过，请重新登录")

    user = db.scalar(select(User).where(User.keycloak_sub == sub))
    if user is not None:
        if user.deleted_at is not None:
            # 账号被软删了就当作不存在，不能凭 Keycloak 的令牌复活它
            raise unauthorized(_BAD_CREDENTIALS)
        return user

    username = (username or "").strip()
    if not username:
        raise unauthorized("身份服务器未提供用户名，无法在本站建立账号")

    user = db.scalar(select(User).where(User.username == username))
    if user is not None:
        if user.keycloak_sub is not None:
            # 这个本地账号已经绑了**另一个** Keycloak 账号，不允许再被认领
            raise conflict("该用户名已绑定其他登录账号，请联系管理员")
        if user.deleted_at is not None:
            raise unauthorized(_BAD_CREDENTIALS)
        user.keycloak_sub = sub
        user.auth_source = "keycloak"
        if email and not user.email:
            user.email = email
        db.commit()
        db.refresh(user)
        return user

    # 用户名有长度上限（String(50)），Keycloak 那边可能更长，截断并去重
    safe_name = username[:50]
    if db.scalar(select(User.id).where(User.username == safe_name)) is not None:
        raise conflict("该用户名已被占用，请联系管理员")

    user = User(
        username=safe_name,
        email=email,
        keycloak_sub=sub,
        auth_source="keycloak",
        password_hash=None,  # SSO 账号本地没有口令
        role="user",  # 写死，绝不让身份服务器决定本站角色
        status="active",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user