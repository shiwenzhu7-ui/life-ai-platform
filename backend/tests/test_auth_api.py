"""B4：认证四接口的端到端测试（注册 → 登录 → 用令牌访问业务接口）。

和 test_auth_guards.py 的区别：那边测「守卫」，这边测「发证」。
本文件全程走真实依赖（清掉 conftest 的 user=1 覆盖），不 mock 任何东西。
"""

import pytest

from conftest import API, new_session
from core.security import hash_password
from modules.shared.users.model import User

PWD = "12345678"


@pytest.fixture()
def open_client(client):
    """清掉 conftest 的「当前用户 = 1」，让请求真的走登录链路。"""
    from app import app as real_app
    from core.deps import get_current_user_id

    real_app.dependency_overrides.clear()
    yield client
    real_app.dependency_overrides[get_current_user_id] = lambda: 1


def _register(client, username="alice", email="a@b.com", password=PWD):
    return client.post(
        f"{API}/auth/register",
        json={"username": username, "email": email, "password": password},
    )


def _login(client, username="alice", password=PWD):
    return client.post(f"{API}/auth/login", json={"username": username, "password": password})


def _token(client, username="alice", password=PWD) -> str:
    r = _login(client, username, password)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ---------- 注册 ----------


def test_register_returns_201_and_hides_password(open_client):
    r = _register(open_client)
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == "alice"
    assert body["role"] == "user", "注册出来的必须是普通用户"
    assert "password" not in body and "password_hash" not in body

    with new_session() as s:
        user = s.query(User).filter_by(username="alice").one()
        assert user.password_hash != PWD, "库里不能存明文"
        assert user.password_hash.startswith("$2b$")


def test_register_duplicate_username_is_409(open_client):
    _register(open_client)
    r = _register(open_client, email="other@b.com")
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"


def test_register_duplicate_email_is_409(open_client):
    _register(open_client)
    r = _register(open_client, username="bob")
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"


def test_register_without_email_twice_is_ok(open_client):
    """MySQL 唯一索引允许多个 NULL，但不允许多个空串 —— 空串必须归一成 None。"""
    assert _register(open_client, username="noemail1", email=None).status_code == 201
    assert _register(open_client, username="noemail2", email=None).status_code == 201
    assert _register(open_client, username="noemail3", email="").status_code == 201


def test_register_cannot_choose_role(open_client):
    """请求体多带 role 直接 422，绝不静默忽略。"""
    r = open_client.post(
        f"{API}/auth/register",
        json={"username": "hacker", "password": PWD, "role": "admin"},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("password", ["short", "1234567"])
def test_register_rejects_short_password(open_client, password):
    r = _register(open_client, password=password)
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_register_rejects_password_over_72_bytes(open_client):
    """25 个汉字 = 75 字节，超 bcrypt 上限。注意 24 个汉字是 72 字节，正好可以。"""
    assert _register(open_client, username="ok24", password="密" * 24).status_code == 201
    r = _register(open_client, username="bad25", password="密" * 25)
    assert r.status_code == 422


def test_register_rejects_bad_username(open_client):
    assert _register(open_client, username="a").status_code == 422
    assert _register(open_client, username="有 空格").status_code == 422


# ---------- 登录 ----------


def test_login_returns_token_and_user(open_client):
    _register(open_client)
    r = _login(open_client)
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 7200
    assert body["user"]["username"] == "alice"
    assert body["user"]["role"] == "user"


def test_login_updates_last_login_at(open_client):
    _register(open_client)
    with new_session() as s:
        assert s.query(User).filter_by(username="alice").one().last_login_at is None

    _login(open_client)

    with new_session() as s:
        assert s.query(User).filter_by(username="alice").one().last_login_at is not None


def test_wrong_password_and_unknown_user_are_indistinguishable(open_client):
    """两条错误必须是同一句文案、同一个状态码，否则能探测用户名是否存在。"""
    _register(open_client)
    wrong_pwd = _login(open_client, password="wrongpass123")
    no_user = _login(open_client, username="nobody")

    assert wrong_pwd.status_code == no_user.status_code == 401
    assert wrong_pwd.json()["message"] == no_user.json()["message"]
    assert wrong_pwd.json()["code"] == no_user.json()["code"] == "UNAUTHORIZED"


def test_disabled_user_with_correct_password_is_403(open_client):
    """密码对了才暴露「被禁用」；反过来的话，不知道密码的人也能探测禁用名单。"""
    _register(open_client)
    with new_session() as s:
        user = s.query(User).filter_by(username="alice").one()
        user.status = "disabled"
        s.commit()

    r = _login(open_client)
    assert r.status_code == 403
    assert r.json()["code"] == "USER_DISABLED"


def test_disabled_user_with_wrong_password_still_401(open_client):
    """密码错 + 账号禁用 → 仍然只说「用户名或密码错误」，不泄露禁用状态。"""
    _register(open_client)
    with new_session() as s:
        user = s.query(User).filter_by(username="alice").one()
        user.status = "disabled"
        s.commit()

    r = _login(open_client, password="wrongpass123")
    assert r.status_code == 401


def test_token_works_on_business_api(open_client):
    """B4 的最终验收：拿真令牌打业务接口，不再是 401。"""
    _register(open_client)
    token = _token(open_client)

    r = open_client.get(f"{API}/tasks", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["total"] == 0

    # 顺便验证「数据隔离」在真实链路下也成立
    r = open_client.post(
        f"{API}/tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "登录后建的待办"},
    )
    assert r.status_code == 201
    r = open_client.get(f"{API}/tasks", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["total"] == 1


# ---------- /auth/me ----------


def test_me_returns_current_user(open_client):
    _register(open_client)
    token = _token(open_client)
    r = open_client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alice"
    assert body["role"] == "user"
    assert "password_hash" not in body, "出参绝不能带口令哈希"


def test_me_has_no_ai_quota_fields(open_client):
    """额度功能已取消（2026-11-10）：字段必须彻底消失，不是返回空值。

    留着字段但恒返回 0/None 会让前端和别人以为「额度还是 0，是不是用完了」，
    所以这里直接断言 key 不存在。
    """
    _register(open_client)
    token = _token(open_client)
    body = open_client.get(
        f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"}
    ).json()

    assert "daily_ai_quota" not in body
    assert "ai_used_today" not in body


def test_me_requires_token(open_client):
    r = open_client.get(f"{API}/auth/me")
    assert r.status_code == 401


# ---------- logout ----------


def test_logout_returns_204_and_token_stays_valid(open_client):
    """JWT 无状态：退出只是前端丢令牌，后端这边令牌在过期前依然有效。
    这条测试把这个「已知行为」钉死，免得以后有人以为它真能吊销。"""
    _register(open_client)
    token = _token(open_client)
    headers = {"Authorization": f"Bearer {token}"}

    assert open_client.post(f"{API}/auth/logout", headers=headers).status_code == 204
    assert open_client.get(f"{API}/auth/me", headers=headers).status_code == 200


def test_logout_requires_token(open_client):
    assert open_client.post(f"{API}/auth/logout").status_code == 401