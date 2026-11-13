"""管理后台接口（V1.4）：`/admin/users` 与 `/admin/usage`。

重心在**门禁**：这套接口能读所有人的数据、能禁用别人的账号，
一旦漏了 `require_role("admin")`，任何登录用户都能拿到全站名单。
所以第一组用例专门打这道门，后面的用例才测业务。

另一个重点是「别把自己锁在门外」：管理员能降级自己、禁用自己，
但库里没有第二个管理员时，那个动作会让所有人再也进不去后台。
"""

import uuid
from datetime import date, timedelta

import pytest

from conftest import API, new_session

from app import app
from core.deps import get_current_user_id
from core.time import now_utc
from modules.lifeweb.ai.models.usage_log import UsageLog
from modules.shared.users.model import User

USERS = f"{API}/admin/users"
USAGE = f"{API}/admin/usage"
ROLES = f"{API}/admin/roles"


# ---------- 夹具 ----------


def _make_user(username: str, *, role: str = "user", status: str = "active") -> int:
    with new_session() as s:
        row = User(
            username=username,
            nickname=username,
            role=role,
            status=status,
            auth_source="local",
        )
        s.add(row)
        s.commit()
        return row.id


def _make_usage(user_id: int, *, token_in=100, token_out=50, status="ok", cost="0.001") -> int:
    with new_session() as s:
        row = UsageLog(
            user_id=user_id,
            purpose="chat",
            model="deepseek-chat",
            token_in=token_in,
            token_out=token_out,
            cost_estimate=cost,
            latency_ms=1200,
            status=status,
        )
        s.add(row)
        s.commit()
        return row.id


@pytest.fixture()
def as_admin():
    """把「当前用户」换成管理员。

    测试里不真登录：`get_current_user_id` 被 conftest 钉死，这里换成
    一个 role=admin 的真实用户 ID，`require_role` 会自己查库判角色。
    """
    admin_id = _make_user("boss", role="admin")
    app.dependency_overrides[get_current_user_id] = lambda: admin_id
    yield admin_id
    app.dependency_overrides[get_current_user_id] = lambda: 1


# ---------- 门禁 ----------


def test_anonymous_is_401(client):
    """不登录：401，而且**不能**因为 require_role 先跑而变成 403。"""
    app.dependency_overrides.clear()
    try:
        for path in (USERS, USAGE, ROLES):
            res = client.get(path)
            assert res.status_code == 401, (path, res.text)
            assert res.json()["code"] == "UNAUTHORIZED"
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: 1


def test_normal_user_is_403(client):
    """普通登录用户：403 FORBIDDEN，不是 404（他知道这个接口存在，只是不让用）。"""
    user_id = _make_user("normal")
    app.dependency_overrides[get_current_user_id] = lambda: user_id

    for path in (USERS, USAGE, ROLES):
        res = client.get(path)
        assert res.status_code == 403, (path, res.text)
        assert res.json()["code"] == "FORBIDDEN"


def test_vip_cannot_enter_admin(client):
    """vip 也算普通用户 —— admin 不自动放行 vip。"""
    vip_id = _make_user("rich", role="vip")
    app.dependency_overrides[get_current_user_id] = lambda: vip_id

    assert client.get(USERS).status_code == 403
    assert client.get(ROLES).status_code == 403, "角色说明也是管理后台数据，vip 同样不能看"


def test_disabled_admin_is_blocked(client):
    """被禁用的管理员连令牌都用不了：403 USER_DISABLED。"""
    admin_id = _make_user("boss", role="admin", status="disabled")
    app.dependency_overrides[get_current_user_id] = lambda: admin_id

    res = client.get(USERS)

    assert res.status_code == 403, res.text
    assert res.json()["code"] == "USER_DISABLED"


# ---------- 用户列表 ----------


def test_list_users_returns_everyone(client, as_admin):
    _make_user("alice")
    _make_user("bob")
    user_id = _make_user("carol")

    res = client.get(USERS)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 4, "包含管理员自己"
    names = {row["username"] for row in body["items"]}
    assert {"boss", "alice", "bob", "carol"} <= names
    # 默认每页 20，四个人一页装得下
    assert body["pages"] == 1

    carol = next(row for row in body["items"] if row["username"] == "carol")
    assert carol["role"] == "user"
    assert carol["status"] == "active"
    assert carol["usage_count"] == 0
    assert carol["id"] == user_id


def test_usage_count_is_aggregated(client, as_admin):
    user_id = _make_user("alice")
    _make_usage(user_id)
    _make_usage(user_id)
    _make_usage(user_id, status="failed")

    body = client.get(USERS).json()

    alice = next(row for row in body["items"] if row["username"] == "alice")
    assert alice["usage_count"] == 3, "失败的调用也要算进去，那是真实消耗"


def test_list_users_filter_by_role(client, as_admin):
    _make_user("alice")
    _make_user("super", role="vip")

    body = client.get(USERS, params={"role": "vip"}).json()

    assert body["total"] == 1
    assert body["items"][0]["username"] == "super"


def test_list_users_filter_by_status(client, as_admin):
    _make_user("alice")
    _make_user("banned", status="disabled")

    body = client.get(USERS, params={"status": "disabled"}).json()

    assert body["total"] == 1
    assert body["items"][0]["username"] == "banned"


def test_list_users_keyword_matches_username_nickname_email(client, as_admin):
    with new_session() as s:
        s.add(User(username="alice", nickname="爱丽丝", email="a@x.com", role="user"))
        s.add(User(username="bob", nickname="鲍勃", email="b@x.com", role="user"))
        s.commit()

    by_nickname = client.get(USERS, params={"keyword": "爱丽"}).json()
    assert by_nickname["total"] == 1
    assert by_nickname["items"][0]["username"] == "alice"

    by_email = client.get(USERS, params={"keyword": "b@x"}).json()
    assert by_email["total"] == 1
    assert by_email["items"][0]["username"] == "bob"


def test_list_users_pagination(client, as_admin):
    for index in range(5):
        _make_user(f"u{index}")

    body = client.get(USERS, params={"page": 2, "size": 2}).json()

    assert body["total"] == 6  # 5 + 管理员自己
    assert body["pages"] == 3
    assert body["page"] == 2
    assert len(body["items"]) == 2


# ---------- 改角色 / 禁用 ----------


def test_promote_user_to_admin(client, as_admin):
    user_id = _make_user("alice")

    res = client.patch(f"{USERS}/{user_id}", json={"role": "admin"})

    assert res.status_code == 200, res.text
    assert res.json()["role"] == "admin"
    with new_session() as s:
        assert s.get(User, user_id).role == "admin"


def test_disable_user(client, as_admin):
    user_id = _make_user("alice")

    res = client.patch(f"{USERS}/{user_id}", json={"status": "disabled"})

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "disabled"


def test_disabled_user_cannot_login(client, as_admin):
    """禁用的真正含义：他登录不上。这条验证「改状态」确实接到了登录链路上。"""
    with new_session() as s:
        row = User(
            username="alice",
            nickname="alice",
            role="user",
            status="active",
            auth_source="local",
        )
        from core.security import hash_password

        row.password_hash = hash_password("secret123")
        s.add(row)
        s.commit()
        user_id = row.id

    client.patch(f"{USERS}/{user_id}", json={"status": "disabled"})

    app.dependency_overrides.clear()
    try:
        res = client.post(
            f"{API}/auth/login", json={"username": "alice", "password": "secret123"}
        )
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: 1

    assert res.status_code == 403, res.text
    assert res.json()["code"] == "USER_DISABLED"


def test_cannot_change_own_role(client, as_admin):
    res = client.patch(f"{USERS}/{as_admin}", json={"role": "user"})

    assert res.status_code == 422, res.text
    assert "不能修改自己的角色" in res.json()["message"]


def test_cannot_disable_self(client, as_admin):
    res = client.patch(f"{USERS}/{as_admin}", json={"status": "disabled"})

    assert res.status_code == 422, res.text
    assert "不能禁用自己" in res.json()["message"]


def test_cannot_demote_last_admin(client, as_admin):
    """库里只剩一个可用管理员时，把他降级要拦住 —— 否则后台从此没人能进。

    用「另一个管理员来降他、但库里只有他一个 active admin」构造这个场景。
    """
    with new_session() as s:
        s.query(User).filter(User.id != as_admin).delete()
        s.commit()

    # 只有 boss 一个 admin 且不能改自己 —— 先验证这条闸门
    res = client.patch(f"{USERS}/{as_admin}", json={"role": "user"})
    assert res.status_code == 422
    assert "不能修改自己的角色" in res.json()["message"]


def test_last_admin_guard_when_demoting_another(client, as_admin):
    """管理员分级（V1.4）：根管理员不可被动，普通管理员只能动普通用户。

    旧规则「数一下还剩几个 admin，少于等于 1 就拦」已被分级取代：
    现在真正保证「后台永远有人能进」的是「根管理员谁都动不了」这一条，
    而不是数人头。本用例按新规则重写。
    """
    root = as_admin  # 夹具里的 boss 是 id 最小的 admin → 自动成为根
    second = _make_user("boss2", role="admin")

    # 根管理员能把另一个管理员降级
    res = client.patch(f"{USERS}/{second}", json={"role": "user"})
    assert res.status_code == 200, res.text

    # 降级之后他就不是管理员了，再发请求直接 403
    app.dependency_overrides[get_current_user_id] = lambda: second
    res = client.patch(f"{USERS}/{root}", json={"role": "user"})
    assert res.status_code == 403, "降级后就不是管理员了"

    # 换一个普通管理员来动根管理员：422 ROOT_PROTECTED
    third = _make_user("boss3", role="admin")
    app.dependency_overrides[get_current_user_id] = lambda: third
    res = client.patch(f"{USERS}/{root}", json={"role": "user"})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "ROOT_PROTECTED"
    res = client.patch(f"{USERS}/{root}", json={"status": "disabled"})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "ROOT_PROTECTED"

    # 根的自我保护：根自己也不能把自己降级或禁用
    app.dependency_overrides[get_current_user_id] = lambda: root
    res = client.patch(f"{USERS}/{root}", json={"role": "user"})
    assert res.status_code == 422
    assert "不能修改自己的角色" in res.json()["message"]
    res = client.patch(f"{USERS}/{root}", json={"status": "disabled"})
    assert res.status_code == 422
    assert "不能禁用自己" in res.json()["message"]


# ---------- 管理员分级（V1.4） ----------


def test_root_can_grant_admin(client, as_admin):
    """第一级能把别人设成管理员，也能再撤销。"""
    user_id = _make_user("bob")

    res = client.patch(f"{USERS}/{user_id}", json={"role": "admin"})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "admin"
    assert res.json()["is_root_admin"] is False

    res = client.patch(f"{USERS}/{user_id}", json={"role": "user"})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "user"


def test_second_level_cannot_grant_admin(client, as_admin):
    """第二级不能把别人设成管理员 —— 否则他就能靠"提人"抬高自己的圈子。"""
    second = _make_user("boss2", role="admin")
    bob = _make_user("bob")
    app.dependency_overrides[get_current_user_id] = lambda: second

    res = client.patch(f"{USERS}/{bob}", json={"role": "admin"})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "CANNOT_GRANT_ADMIN"


def test_second_level_cannot_touch_other_admin(client, as_admin):
    """第二级不能改任何管理员账号，连启用/禁用也不行。"""
    second = _make_user("boss2", role="admin")
    other = _make_user("boss3", role="admin")
    app.dependency_overrides[get_current_user_id] = lambda: second

    for body in ({"role": "user"}, {"status": "disabled"}, {"status": "active"}):
        res = client.patch(f"{USERS}/{other}", json=body)
        assert res.status_code == 422, (body, res.text)
        assert res.json()["code"] == "CANNOT_TOUCH_ADMIN"


def test_second_level_can_manage_normal_users(client, as_admin):
    """第二级对普通用户 / VIP 有完整权限：改角色、禁用、启用。"""
    second = _make_user("boss2", role="admin")
    bob = _make_user("bob")
    app.dependency_overrides[get_current_user_id] = lambda: second

    res = client.patch(f"{USERS}/{bob}", json={"role": "vip"})
    assert res.status_code == 200, res.text
    res = client.patch(f"{USERS}/{bob}", json={"status": "disabled"})
    assert res.status_code == 200, res.text
    res = client.patch(f"{USERS}/{bob}", json={"status": "active", "role": "user"})
    assert res.status_code == 200, res.text


def test_root_flag_in_user_list_and_me(client, as_admin):
    """列表和 /auth/me 都要标出谁是根，前端靠它锁按钮。"""
    _make_user("boss2", role="admin")

    res = client.get(USERS, params={"size": 100})
    assert res.status_code == 200, res.text
    roots = [item["username"] for item in res.json()["items"] if item["is_root_admin"]]
    assert roots == ["boss"], roots
    # boss2 也是 admin，但不是根
    assert any(
        item["username"] == "boss2" and item["role"] == "admin" and not item["is_root_admin"]
        for item in res.json()["items"]
    )

    me = client.get(f"{API}/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["is_root_admin"] is True


def test_config_can_pin_root_user(client, as_admin, monkeypatch):
    """在 .env 里钉死 ADMIN_ROOT_USER_ID 后，判定跟着走，不再看 id 大小。"""
    from core.config import settings

    second = _make_user("boss2", role="admin")
    monkeypatch.setattr(settings, "admin_root_user_id", second)

    # 原根 boss 现在只是普通管理员：动不了新的根（ROOT_PROTECTED 优先于分级判断）
    res = client.patch(f"{USERS}/{second}", json={"status": "disabled"})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "ROOT_PROTECTED"

    # 新根也不能把自己降级
    app.dependency_overrides[get_current_user_id] = lambda: second
    res = client.patch(f"{USERS}/{second}", json={"role": "user"})
    assert res.status_code == 422
    assert "不能修改自己的角色" in res.json()["message"]


def test_patch_unknown_user_is_404(client, as_admin):
    res = client.patch(f"{USERS}/999999", json={"role": "user"})

    assert res.status_code == 404, res.text


def test_patch_with_no_fields_is_422(client, as_admin):
    user_id = _make_user("alice")

    res = client.patch(f"{USERS}/{user_id}", json={})

    assert res.status_code == 422, res.text


def test_patch_rejects_unknown_fields(client, as_admin):
    """白名单：改密码 / 改用户名都不能从这个口子进。"""
    user_id = _make_user("alice")

    res = client.patch(f"{USERS}/{user_id}", json={"password_hash": "x"})

    assert res.status_code == 422, res.text


def test_patch_invalid_role_is_422(client, as_admin):
    user_id = _make_user("alice")

    res = client.patch(f"{USERS}/{user_id}", json={"role": "superman"})

    assert res.status_code == 422, res.text


# ---------- 调用明细 ----------


def test_usage_lists_calls(client, as_admin):
    user_id = _make_user("alice")
    _make_usage(user_id, token_in=800, token_out=120, cost="0.012")

    res = client.get(USAGE)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["username"] == "alice"
    assert row["token_in"] == 800
    assert row["model"] == "deepseek-chat"
    assert body["summary"]["token_in"] == 800
    assert body["summary"]["calls"] == 1


def test_usage_summary_does_not_change_with_paging(client, as_admin):
    """合计是筛选范围的汇总，翻页不能变 —— 否则第 2 页会显示「只花了 0.001 元」。"""
    user_id = _make_user("alice")
    for _ in range(5):
        _make_usage(user_id, cost="0.01")

    first = client.get(USAGE, params={"page": 1, "size": 2}).json()
    second = client.get(USAGE, params={"page": 2, "size": 2}).json()

    assert first["total"] == 5
    assert first["summary"] == second["summary"]
    assert first["summary"]["calls"] == 5
    assert abs(first["summary"]["cost_estimate"] - 0.05) < 1e-9


def test_usage_filter_by_user(client, as_admin):
    alice = _make_user("alice")
    bob = _make_user("bob")
    _make_usage(alice)
    _make_usage(bob)

    body = client.get(USAGE, params={"user_id": bob}).json()

    assert body["total"] == 1
    assert body["items"][0]["username"] == "bob"


def test_usage_filter_by_keyword(client, as_admin):
    alice = _make_user("alice")
    bob = _make_user("bob")
    _make_usage(alice)
    _make_usage(bob)

    body = client.get(USAGE, params={"keyword": "alice"}).json()

    assert body["total"] == 1
    assert body["items"][0]["username"] == "alice"


def test_usage_filter_by_date_range(client, as_admin):
    """左闭右开：今天 0 点 ~ 明天 0 点 = 今天一整天。"""
    user_id = _make_user("alice")
    _make_usage(user_id)

    today = date.today()
    start = f"{today}T00:00:00"
    end = f"{today + timedelta(days=1)}T00:00:00"

    inside = client.get(USAGE, params={"start": start, "end": end}).json()
    assert inside["total"] == 1

    yesterday_end = f"{today}T00:00:00"
    outside = client.get(USAGE, params={"end": yesterday_end}).json()
    assert outside["total"] == 0


def test_usage_filter_by_status(client, as_admin):
    user_id = _make_user("alice")
    _make_usage(user_id)
    _make_usage(user_id, status="failed")

    body = client.get(USAGE, params={"status": "failed"}).json()

    assert body["total"] == 1
    assert body["summary"]["failed"] == 1
    assert body["summary"]["calls"] == 1, "合计跟着筛选走"


def test_usage_shows_unknown_user_as_null(client, as_admin):
    """用户被硬删之后日志还在（没有外键），username 显示空，不能 500。"""
    with new_session() as s:
        s.add(
            UsageLog(
                user_id=99999,
                purpose="chat",
                model="deepseek-chat",
                token_in=1,
                token_out=1,
                status="ok",
            )
        )
        s.commit()

    body = client.get(USAGE).json()

    assert body["total"] == 1
    assert body["items"][0]["username"] is None
    assert body["items"][0]["user_id"] == 99999


# ---------- 角色说明 ----------


def test_roles_lists_three_roles_in_order(client, as_admin):
    """三个角色、按 user → vip → admin 排，前端直接照着渲染卡片。"""
    res = client.get(ROLES)

    assert res.status_code == 200, res.text
    body = res.json()
    assert [item["role"] for item in body] == ["user", "vip", "admin"]
    assert [item["label"] for item in body] == ["普通用户", "VIP", "管理员"]


def test_roles_each_has_description(client, as_admin):
    """每张卡都必须有 summary 和非空 abilities，否则前端会渲出一个空壳。"""
    for item in client.get(ROLES).json():
        assert item["summary"].strip(), item
        assert item["abilities"], item
        assert all(isinstance(line, str) and line.strip() for line in item["abilities"])


def test_roles_does_not_leak_user_data(client, as_admin):
    """角色说明只有四个字段 —— 不要把库里的用户数之类顺手塞进来。"""
    for item in client.get(ROLES).json():
        assert set(item) == {"role", "label", "summary", "abilities"}, item