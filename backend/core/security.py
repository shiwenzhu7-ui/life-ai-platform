"""口令哈希与 JWT —— 「怎么证明你是谁」的唯一实现。

分工：
    hash_password / verify_password  口令进库前单向哈希，库里永远不存明文
    create_access_token             登录成功后签发通行证
    decode_token                    每个请求把通行证换回 user_id

谁调用它：
    B4 的认证接口（注册 / 登录）用口令那一对；
    B3 的 get_current_user_id 用 decode_token。

为什么不用 passlib：
    passlib 已停止维护，和 bcrypt>=4.1 一起装会报 `__about__` 错误
    （见 docs/工程规范.md §2.1 的踩坑表）。改用 pwdlib —— 它就是 passlib 的
    官方继任者，接口更小，只保留「哈希 / 校验」两件事。

注意：这里只做密码学，不做业务判断。
    用户是否存在、账号是否禁用、角色够不够，一律留给调用方。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

from core.config import settings
from core.errors import unauthorized

# bcrypt 只处理口令的前 72 字节，多出来的部分会被**静默忽略**。
# bcrypt 5.x 改成直接抛 ValueError（老版本悄悄截断）。
# 两种都不好：截断会让「72 字节以内相同、之后不同」的两个口令互相能登录。
# 所以这里自己先拦：超长就明确报错，让注册接口去提示用户。
_BCRYPT_MAX_BYTES = 72

# 显式指定 bcrypt，不用 PasswordHash.recommended()：
# recommended() 会先找 argon2，没装 argon2-cffi 时直接抛 HasherNotAvailable。
# 只保留一种算法也意味着「库里的哈希格式只有一种」，排查时不用猜。
_password_hash = PasswordHash((BcryptHasher(),))


def _encode(password: str) -> bytes:
    """口令转字节。统一用 utf-8 —— bcrypt 的 72 字节是按字节算的，不是按字数。"""
    return password.encode("utf-8")


def hash_password(password: str) -> str:
    """明文 → bcrypt 哈希（自带随机盐，同一口令每次结果都不同）。

    超 72 字节抛 ValueError，由调用方（注册接口）转成 422。
    这里不直接抛 AppError：这是「参数不合法」，不是「业务状态不对」，
    校验层比 service 层更该管这件事。
    """
    raw = _encode(password)
    if len(raw) > _BCRYPT_MAX_BYTES:
        raise ValueError(f"密码过长：最多 {_BCRYPT_MAX_BYTES} 字节（约 24 个汉字）")
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """比对明文与库里存的哈希。

    - 对方没有口令（`password_hash` 为空，比如 V2.0 起由 Keycloak 管的账号）→ False
    - 哈希串损坏 / 算法不认识 → False（pwdlib 自己吞掉异常，这里再兜一层）
    - 任何情况下都**只返回布尔**，不把「为什么失败」泄露给调用方
    """
    if not password_hash:
        return False
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:  # noqa: BLE001 —— 库里的哈希可能来自别的算法，不能让它把登录搞挂
        return False


def _now() -> datetime:
    """当前 UTC 时间。时间基准全项目统一走这里，和 core/time.py 的口径一致。"""
    return datetime.now(UTC)


def _require_secret() -> str:
    """密钥没配就拒绝签发。

    和 core/db.py 对 DATABASE_URL 的处理一个思路：与其拿空串签出一个
    「谁都能伪造」的令牌，不如在第一次登录时就明确失败。
    """
    if not settings.jwt_secret.strip():
        raise RuntimeError("找不到 JWT_SECRET，请检查 backend/.env（见 .env.example）")
    return settings.jwt_secret


def create_access_token(user_id: int, expires_minutes: int | None = None) -> str:
    """签发 Access Token。

    载荷只有三个：
        sub  用户 ID —— **必须是字符串**。RFC 7519 规定 sub 是 StringOrURI，
             PyJWT 2.10 起对非字符串 sub 会告警/报错；V2.0 换成 Keycloak 时
             sub 本来就是 UUID 字符串，现在按字符串存，将来不用改表也不用改解析。
        iat  签发时间
        exp  过期时间 = iat + JWT_EXPIRE_MINUTES

    不往令牌里塞 role / nickname：
        令牌一签出去就无法撤回，角色改了也要等到过期才生效。
        角色从数据库实时查（B3 的 get_current_user 负责），令牌只承担「你是谁」。
    """
    now = _now()
    minutes = settings.jwt_expire_minutes if expires_minutes is None else expires_minutes
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, _require_secret(), algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> int:
    """通行证 → user_id。失败一律 401（不区分「过期」和「伪造」）。

    不区分是有意的：给攻击者区分「签名对了只是过期」和「签名不对」的信息，
    等于告诉他哪部分猜对了。日志里可以分清，响应体里不分。

    PyJWT 的异常几乎都继承 InvalidTokenError：
        ExpiredSignatureError  过期
        InvalidSignatureError  签名不对（密钥换了、被篡改）
        DecodeError / InvalidTokenError  不是 JWT、格式坏
    """
    try:
        payload = jwt.decode(
            token,
            _require_secret(),
            algorithms=[settings.jwt_algorithm],  # 必须写死允许的算法，否则 "alg":"none" 能被绕过
            options={"require": ["exp", "sub"]},  # 缺 exp 或 sub 的令牌直接拒
        )
    except jwt.InvalidTokenError:
        raise unauthorized("登录已失效，请重新登录") from None

    sub = payload.get("sub")
    try:
        return int(sub)
    except (TypeError, ValueError):
        # sub 不是数字：令牌是我们自己签的，出现这种情况只可能是密钥泄露后被伪造
        raise unauthorized("登录已失效，请重新登录") from None