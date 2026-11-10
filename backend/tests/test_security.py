"""口令哈希与 JWT 的单元测试（V1.2 B2）。

只测 core/security.py 本身，不碰 HTTP、不碰数据库：
    口令 → 哈希 → 校验
    用户 ID → 令牌 → 用户 ID
    任何一步被篡改 / 过期 / 密钥没配，都必须明确失败

接口层的 401/403 在 B3/B6 再测（tests/test_auth.py、tests/test_isolation.py）。
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from core import security
from core.config import settings
from core.errors import AppError


# ---------- 口令 ----------


def test_hash_is_salted_and_roundtrips():
    """同一口令两次哈希结果不同（有随机盐），但都能验过。"""
    first = security.hash_password("demo1234")
    second = security.hash_password("demo1234")

    assert first != second, "bcrypt 每次都要带新盐，否则相同口令会得到相同哈希"
    assert first.startswith("$2b$"), "bcrypt 5.x 产出的版本前缀"
    assert security.verify_password("demo1234", first) is True
    assert security.verify_password("demo1234", second) is True


def test_verify_rejects_wrong_password():
    hashed = security.hash_password("demo1234")
    assert security.verify_password("demo12345", hashed) is False
    assert security.verify_password("", hashed) is False


def test_verify_handles_missing_and_broken_hash():
    """没有口令（Keycloak 账号）和坏哈希都只返回 False，不抛异常。"""
    assert security.verify_password("demo1234", None) is False
    assert security.verify_password("demo1234", "") is False
    assert security.verify_password("demo1234", "不是 bcrypt 哈希") is False


def test_hash_rejects_over_72_bytes():
    """bcrypt 只看前 72 字节，超长必须在入口拦住，不能截断。"""
    ok = "密" * 24  # 24 个汉字 = 72 字节
    assert security.verify_password(ok, security.hash_password(ok)) is True

    with pytest.raises(ValueError, match="密码过长"):
        security.hash_password("密" * 25)


def test_demo_seed_hash_matches_migration():
    """迁移 B1 里写死的演示用户哈希必须就是 demo1234。

    迁移文件不在包里（alembic/versions 没有 __init__.py，按路径按需加载），
    所以用 importlib 直接读文件，不经过包导入。
    """
    from pathlib import Path

    import importlib.util

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "b1users_add_auth_columns.py"
    spec = importlib.util.spec_from_file_location("b1users_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert security.verify_password(module.DEMO_PASSWORD, module.DEMO_PASSWORD_HASH) is True
    assert security.verify_password("demo1235", module.DEMO_PASSWORD_HASH) is False


# ---------- 令牌 ----------


def test_token_roundtrip():
    token = security.create_access_token(7)
    assert isinstance(token, str) and token.count(".") == 2
    assert security.decode_token(token) == 7


def test_token_payload_uses_string_sub():
    """sub 必须是字符串（RFC 7519），否则 PyJWT 2.10+ 和 Keycloak 都对不上。"""
    token = security.create_access_token(42)
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert payload["sub"] == "42"
    assert isinstance(payload["iat"], int)
    assert payload["exp"] - payload["iat"] == settings.jwt_expire_minutes * 60


def test_expired_token_rejected():
    past = datetime.now(UTC) - timedelta(minutes=1)
    token = jwt.encode(
        {"sub": "1", "iat": past - timedelta(minutes=120), "exp": past},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(AppError) as exc:
        security.decode_token(token)
    assert exc.value.code == "UNAUTHORIZED"
    assert exc.value.status_code == 401


def test_tampered_token_rejected():
    token = security.create_access_token(1)
    head, body, sig = token.split(".")
    # 改签名最后一个字符
    bad_sig = head + "." + body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")
    for broken in (bad_sig, head + "." + body + ".", "not-a-jwt", ""):
        with pytest.raises(AppError) as exc:
            security.decode_token(broken)
        assert exc.value.code == "UNAUTHORIZED"


def test_token_signed_with_other_secret_rejected():
    forged = jwt.encode(
        {"sub": "1", "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "someone-elses-secret",
        algorithm="HS256",
    )
    with pytest.raises(AppError):
        security.decode_token(forged)


def test_token_without_exp_or_sub_rejected():
    """缺 exp / 缺 sub 的令牌不能当有效期无限、也不能当匿名用户放行。"""
    now = datetime.now(UTC)
    no_exp = jwt.encode({"sub": "1"}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    no_sub = jwt.encode(
        {"exp": now + timedelta(minutes=5)}, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    bad_sub = jwt.encode(
        {"sub": "abc", "exp": now + timedelta(minutes=5)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    for token in (no_exp, no_sub, bad_sub):
        with pytest.raises(AppError):
            security.decode_token(token)


def test_alg_none_token_rejected():
    """经典绕过手法：alg=none 的空签名令牌。algorithms 写死成 HS256 才挡得住。"""
    unsigned = jwt.encode({"sub": "1"}, key="", algorithm="none")
    with pytest.raises(AppError):
        security.decode_token(unsigned)


def test_missing_secret_refuses_to_sign(monkeypatch):
    """JWT_SECRET 没配时宁可不签，也不能拿空串签。"""
    monkeypatch.setattr(settings, "jwt_secret", "")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        security.create_access_token(1)