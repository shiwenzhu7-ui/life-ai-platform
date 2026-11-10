"""认证业务逻辑（V1.2）。

这里只做「决定」：这个人能不能注册、这对账号口令对不对、他今天用了多少次 AI。
HTTP 状态码由 router 翻译 —— service 抛 AppError，router 不写 if。

两条安全约定：
    1. 登录失败永远是同一句话，不区分「用户不存在」「密码错」「口令为空」。
       区分了就等于把「哪些用户名存在」免费送给攻击者。
    2. 禁用判断放在**密码校验之后**。反过来的话，不知道密码的人也能探测出
       哪些账号被禁用了。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import settings
from core.errors import conflict, unauthorized, validation_error
from core.security import create_access_token, hash_password, verify_password
from core.time import now_utc
from modules.lifeweb.ai.models import UsageLog
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
        user=UserBrief.model_validate(user),
    )


def ai_used_today(db: Session, user_id: int) -> int:
    """今天（服务器本地日期）成功的 AI 调用次数。

    口径：`ai_usage_logs` 里 status='ok' 的行数 —— 即「模型调用次数」，
    不是「对话轮数」：一次提问触发工具循环会算多次，摘要调用另算一次。
    失败的调用**不计**：用户没拿到服务却被扣额度不合理；
    靠故意制造失败来刷额度的问题应该由限流解决，不该用额度去堵。

    SQL 用范围条件（>= 今天 0 点）而不是 DATE(created_at)=CURDATE()：
    后者会让 idx_usage_user_time 的 created_at 部分失效，退化成扫全表。
    created_at 由 MySQL 的 NOW() 写入，CURDATE() 与之同一时区，不会错位。
    """
    return db.scalar(
        select(func.count())
        .select_from(UsageLog)
        .where(
            UsageLog.user_id == user_id,
            UsageLog.created_at >= func.curdate(),
            UsageLog.status == "ok",
        )
    ) or 0


def me(db: Session, user: User) -> UserOut:
    """当前用户信息。role/status 由依赖层校验过（get_current_user）。"""
    return UserOut(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
        role=user.role,
        daily_ai_quota=user.daily_ai_quota,
        ai_used_today=ai_used_today(db, user.id),
    )