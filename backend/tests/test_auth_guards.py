"""B3：JWT 门禁本身的测试 —— 走**真实**依赖，不用 conftest 的 override。

其他测试文件都靠 `app.dependency_overrides[get_current_user_id] = lambda: 1`
绕过登录；本文件把它清掉，验证「没令牌就进不去」这条线真的成立。

对应需求 docs/需求规格说明书.md:379-383：
    未登录 401 / 令牌过期或伪造 401 / 禁用账号 403 / 角色不够 403

注意：/auth/me 是 B4 才有的接口。凡是需要 get_current_user（查库那层）的用例，
统一打 _probe_client() 这个临时 app，B4 落地后再改成打正式接口。
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from conftest import API, new_session
from core.config import settings
from core.deps import get_current_user, get_current_user_id, require_role
from core.errors import register_exception_handlers, unauthorized
from core.security import create_access_token
from modules.lifeweb.tasks.model import Task
from modules.shared.users.model import User


@pytest.fixture()
def raw_client(client):
    """清掉 conftest 的用户覆盖，让请求真的走 Authorization 头。"""
    from app import app as real_app

    real_app.dependency_overrides.clear()
    yield client
    real_app.dependency_overrides[get_current_user_id] = lambda: 1


def _make_user(*, role: str = "user", status: str = "active", **kw) -> int:
    """直接写一个用户，返回 id。用户名带时间戳避免唯一键冲突。"""
    with new_session() as s:
        user = User(
            username=f"guards_{datetime.now(UTC).timestamp()!r}_{role}_{status}",
            role=role,
            status=status,
            **kw,
        )
        s.add(user)
        s.commit()
        return user.id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def _probe_client():
    """临时 app：只挂 get_current_user / require_role 两个依赖，测完即弃。"""
    probe = FastAPI()
    register_exception_handlers(probe)
    router = APIRouter()

    @router.get("/me")
    def me(user: User = Depends(get_current_user)):
        return {"id": user.id, "role": user.role}

    @router.get("/only-admin")
    def only_admin(user: User = Depends(require_role("admin"))):
        return {"role": user.role}

    @router.get("/either")
    def either(user: User = Depends(require_role("user", "admin"))):
        return {"role": user.role}

    probe.include_router(router)
    with TestClient(probe) as c:
        yield c


# ---------- 401：没带 / 带错令牌 ----------


def test_missing_header_is_401(raw_client):
    r = raw_client.get(f"{API}/tasks")
    assert r.status_code == 401, "没登录必须是 401，不能是 FastAPI 默认的 403"
    assert r.json()["code"] == "UNAUTHORIZED"
    # RFC 7235：401 必须带质询头，前端拦截器靠它判断「该跳登录」
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_empty_bearer_is_401(raw_client):
    r = raw_client.get(f"{API}/tasks", headers={"Authorization": "Bearer "})
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_garbage_token_is_401(raw_client):
    r = raw_client.get(f"{API}/tasks", headers=_auth("not-a-jwt"))
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_expired_token_is_401(raw_client):
    past = datetime.now(UTC) - timedelta(minutes=1)
    expired = jwt.encode(
        {"sub": "1", "exp": past}, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    r = raw_client.get(f"{API}/tasks", headers=_auth(expired))
    assert r.status_code == 401


def test_valid_token_but_user_missing_is_401(raw_client):
    """签名对、但 sub 指向不存在的用户：让重新登录，不报 403。"""
    with _probe_client() as c:
        r = c.get("/me", headers=_auth(create_access_token(999999)))
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_token_of_deleted_user_is_401(raw_client):
    uid = _make_user(deleted_at=datetime.now(UTC).replace(tzinfo=None))
    with _probe_client() as c:
        r = c.get("/me", headers=_auth(create_access_token(uid)))
    assert r.status_code == 401


def test_valid_token_passes_and_sees_only_own_data(raw_client):
    """真令牌能进业务接口，而且只看见自己的数据。"""
    uid = _make_user()
    other = _make_user()
    with new_session() as s:
        s.add(Task(user_id=uid, title="我的待办", status="todo", priority="medium"))
        s.add(Task(user_id=other, title="别人的待办", status="todo", priority="medium"))
        s.commit()

    r = raw_client.get(f"{API}/tasks", headers=_auth(create_access_token(uid)))
    assert r.status_code == 200
    assert [item["title"] for item in r.json()["items"]] == ["我的待办"]


# ---------- 403：身份有效但被禁用 / 权限不够 ----------


def test_disabled_user_is_403(raw_client):
    """禁用账号要走到「查库」那一层才拦得住（get_current_user）。"""
    uid = _make_user(status="disabled")
    with _probe_client() as c:
        r = c.get("/me", headers=_auth(create_access_token(uid)))
    assert r.status_code == 403
    assert r.json()["code"] == "USER_DISABLED"


def test_require_role_rejects_wrong_role(raw_client):
    plain_id = _make_user(role="user")
    with _probe_client() as c:
        r = c.get("/only-admin", headers=_auth(create_access_token(plain_id)))
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


def test_require_role_allows_listed_role(raw_client):
    admin_id = _make_user(role="admin")
    with _probe_client() as c:
        r = c.get("/only-admin", headers=_auth(create_access_token(admin_id)))
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_require_role_accepts_any_listed(raw_client):
    """require_role 多角色是「满足其一」。"""
    user_id = _make_user(role="user")
    with _probe_client() as c:
        r = c.get("/either", headers=_auth(create_access_token(user_id)))
    assert r.status_code == 200


def test_unauthorized_error_carries_www_authenticate():
    err = unauthorized()
    assert err.status_code == 401
    assert err.headers == {"WWW-Authenticate": "Bearer"}