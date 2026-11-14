"""V2.0 SSO：OIDC 授权码流程 + 本地账号落库。

这一组测试**不打真实的 Keycloak** —— 那样每次跑测试都要先起虚拟机里的容器、
还得手工登录一次拿 code，CI 和学习机上都没法跑。改成两处替换：

    换 token：mock `oidc.exchange_code`，返回我们手搓的 id_token
    验签名：mock `oidc._get_jwks_client`，让它交出本地生成的 RSA 公钥

于是「验签名 / 验 iss / 验 aud / 验 nonce / 验过期」这些**代码路径都是真的**，
伪造的只是「Keycloak 那台机器的存在」。凡是 Keycloak 会给的东西（claims 内容、
签名密钥、错误码），都在用例里显式写出来，看得见才算数。

覆盖的失败路径（每一条都对应一个真实会踩的场景）：
    state 缺失 / 过期 / 被篡改        → 回到前端 ?sso_error=state_*
    用户在 Keycloak 上点了取消         → ?sso_error=access_denied
    换 token 被拒（code 重放/secret 错）→ ?sso_error=sso_token_rejected
    id_token 签名伪造                  → ?sso_error=sso_bad_id_token
    id_token 的 nonce 对不上（重放）    → ?sso_error=sso_bad_nonce
    账号被禁用                         → ?sso_error=user_disabled
"""

import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core.config import settings
from core.errors import AppError
from core.security import decode_token
from modules.shared.auth import oidc, sso_state
from modules.shared.auth import service as auth_service
from modules.shared.users.model import User

SSO_LOGIN = "/api/v1/auth/sso/login"
SSO_CALLBACK = "/api/v1/auth/sso/callback"
FRONTEND_PATH = "/sso/callback"


# ---------- 夹具：一对假的 Keycloak 签名密钥 ----------


@pytest.fixture(scope="module")
def ks_key():
    """模拟 Keycloak 的 RS256 签名密钥（只在本测试进程里存在）。"""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeSigningKey:
    """PyJWKClient.get_signing_key_from_jwt 的返回值形状：只要有个 .key。"""

    def __init__(self, key):
        self.key = key


@pytest.fixture()
def fake_keycloak(monkeypatch, ks_key):
    """把「去 Keycloak 验签名」换成本地公钥，其余逻辑一律保留真实实现。"""

    class _Client:
        def get_signing_key_from_jwt(self, _token):  # noqa: ANN001
            return _FakeSigningKey(ks_key.public_key())

    monkeypatch.setattr(oidc, "_get_jwks_client", lambda: _Client())
    return ks_key


@pytest.fixture()
def sso_env(monkeypatch):
    """保证 SSO 处于「已配置」状态，不被本机 .env 影响。"""
    monkeypatch.setattr(settings, "oidc_issuer", "http://kc.test/realms/lifeai")
    monkeypatch.setattr(settings, "oidc_client_id", "")
    monkeypatch.setattr(settings, "oidc_client_secret", "")
    monkeypatch.setattr(
        settings,
        "oidc_clients",
        "life-web=life-web:secret-abcdefghijklmnopqrstuvwxyz123456,"
        "admin-web=admin-web:admin-secret-abcdefghijklmnopqrstuvwxyz",
    )
    monkeypatch.setattr(
        settings, "oidc_redirect_uri", "http://localhost:8010/api/v1/auth/sso/callback"
    )
    monkeypatch.setattr(settings, "oidc_frontend_callback", "http://localhost:8080/sso/callback")
    monkeypatch.setattr(
        settings,
        "oidc_frontend_callbacks",
        "life-web=http://localhost:8080/sso/callback,admin-web=http://localhost:8082/sso/callback",
    )
    return settings


def make_id_token(
    ks_key, *, nonce, sub="kc-sub-1", username="sso_alice", app="life-web", **overrides
) -> str:
    """手搓一个「Keycloak 会发出来的」id_token（aud 是这个 app 的 client_id）。"""
    now = int(time.time())
    client_id = settings.oidc_client_of(app)[0]
    claims = {
        "iss": settings.oidc_issuer.rstrip("/"),
        "aud": client_id,
        "azp": client_id,
        "sub": sub,
        "preferred_username": username,
        "email": f"{username}@example.com",
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
    }
    claims.update(overrides)
    return jwt.encode(claims, ks_key, algorithm="RS256")


def put_state_cookie(
    client, *, app="life-web", state="s1", verifier="v1", nonce="n1", next_path=""
) -> str:
    """生成一次性票据并**以 Cookie 头的形式**带给回调。

    放弃 client.cookies.set 的原因：Starlette TestClient 会把 base_url 的
    主机（testserver）重写成 client 的 base_url，走 cookiejar 时匹配不上，
    请求头里根本没有 Cookie —— 表现是「所有用例都返回 state_expired」，
    排查起来极费时间。直接拼 header 最贴近浏览器的真实行为，也最不容易误判。
    """
    value = sso_state.issue(
        app=app, state=state, code_verifier=verifier, nonce=nonce, next_path=next_path
    )
    return {settings.oidc_cookie_name: value}


def parse_redirect(resp) -> dict:
    """把 302 的目标拆成 {path, q}：q 是 query 的一层字典。"""
    parsed = urlparse(resp.headers["location"])
    return {"path": parsed.path, "q": {k: v[0] for k, v in parse_qs(parsed.query).items()}}


# ---------- 1. /sso/login：302 + Cookie ----------


def test_sso_login_redirects_and_sets_state_cookie(client, sso_env):
    res = client.get(SSO_LOGIN, follow_redirects=False)
    assert res.status_code == 302, res.text

    loc = res.headers["location"]
    assert loc.startswith("http://kc.test/realms/lifeai/protocol/openid-connect/auth?")
    q = {k: v[0] for k, v in parse_qs(urlparse(loc).query).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == "life-web"
    assert q["redirect_uri"] == "http://localhost:8010/api/v1/auth/sso/callback"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] and "=" not in q["code_challenge"], "BASE64URL 必须去掉 ="
    assert q["nonce"] and q["state"]
    assert "client_secret" not in loc, "secret 绝不能出现在浏览器能看到的 URL 里"

    cookie = res.cookies.get(settings.oidc_cookie_name)
    assert cookie, "state 票据必须写进 Cookie"
    saved = sso_state.read(cookie)
    assert saved["state"] == q["state"]
    assert oidc.code_challenge_of(saved["cv"]) == q["code_challenge"], "PKCE 必须能算回去"


def test_sso_login_503_when_not_configured(client, monkeypatch, sso_env):
    monkeypatch.setattr(settings, "oidc_clients", "")
    res = client.get(SSO_LOGIN, follow_redirects=False)
    assert res.status_code == 503
    assert res.json()["code"] == "SSO_NOT_CONFIGURED"


def test_sso_login_503_when_app_not_configured(client, sso_env):
    """admin-web 的密钥没配时，管理端不能发起登录，但生活端不受影响。"""
    res = client.get(f"{SSO_LOGIN}?app=analytics-web", follow_redirects=False)
    assert res.status_code == 503
    assert res.json()["code"] == "SSO_NOT_CONFIGURED"
    assert client.get(SSO_LOGIN, follow_redirects=False).status_code == 302


def test_sso_login_uses_client_of_the_calling_app(client, sso_env):
    """admin-web 发起的登录必须用 admin-web 的 client_id，并把 app 签进票据。"""
    res = client.get(f"{SSO_LOGIN}?app=admin-web", follow_redirects=False)
    assert res.status_code == 302

    q = {k: v[0] for k, v in parse_qs(urlparse(res.headers["location"]).query).items()}
    assert q["client_id"] == "admin-web", "用错了客户端，Keycloak 会按 redirect_uri 拒掉"

    saved = sso_state.read(res.cookies.get(settings.oidc_cookie_name))
    assert saved["app"] == "admin-web", "回调要靠这张票知道用哪套 secret、回哪个前端"


def test_callback_of_admin_web_goes_back_to_admin_web(client, sso_env, fake_keycloak, monkeypatch):
    """管理端登录走完后要回 8082，而不是生活端的 8080。"""
    token = make_id_token(fake_keycloak, nonce="n1", app="admin-web", username="sso_admin")
    monkeypatch.setattr(oidc, "exchange_code", lambda **_: {"id_token": token})

    jar = put_state_cookie(client, app="admin-web")
    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)

    info = parse_redirect(res)
    assert info["path"] == "/sso/callback"
    assert res.headers["location"].startswith("http://localhost:8082/"), res.headers["location"]
    assert "token" in info["q"]


def test_callback_uses_exchange_of_the_app_in_cookie(client, sso_env, fake_keycloak, monkeypatch):
    """换 token 时用的 app 必须来自票据，不能来自回调 URL 上的参数。"""
    seen: list[str] = []
    token = make_id_token(fake_keycloak, nonce="n1", app="admin-web", username="sso_admin")
    monkeypatch.setattr(
        oidc, "exchange_code", lambda **kw: seen.append(kw["app"]) or {"id_token": token}
    )

    jar = put_state_cookie(client, app="admin-web")
    # 攻击者试图在回调 URL 上指定用 life-web 的凭据
    res = client.get(
        f"{SSO_CALLBACK}?code=abc&state=s1&app=life-web", cookies=jar, follow_redirects=False
    )

    assert seen == ["admin-web"], "app 只认票据里的那一份，URL 参数不参与"
    assert "token" in parse_redirect(res)["q"]


def test_callback_rejects_id_token_of_another_client(client, sso_env, fake_keycloak, monkeypatch):
    """aud 是 life-web 的 id_token 不能拿来登 admin-web。"""
    token = make_id_token(fake_keycloak, nonce="n1", app="life-web", username="sso_admin")
    monkeypatch.setattr(oidc, "exchange_code", lambda **_: {"id_token": token})

    jar = put_state_cookie(client, app="admin-web")
    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)
    assert res.headers["location"].startswith("http://localhost:8082/")
    assert parse_redirect(res)["q"]["sso_error"] == "sso_bad_id_token"


# ---------- 2. 失败路径：都回前端 ?sso_error=xxx ----------


def test_callback_without_cookie_goes_back_with_state_expired(client, sso_env, db):
    res = client.get(f"{SSO_CALLBACK}?code=abc&state=xyz", follow_redirects=False)
    assert res.status_code == 302
    info = parse_redirect(res)
    assert info["path"] == FRONTEND_PATH
    assert info["q"]["sso_error"] == "state_expired"
    assert db.query(User).count() == 0, "失败的登录不能建号"


def test_callback_with_tampered_cookie_is_rejected(client, sso_env, db):
    good = sso_state.issue(app="life-web", state="s1", code_verifier="v1", nonce="n1")
    tampered = good[:-1] + ("A" if good[-1] != "A" else "B")  # 改一个字符 → 签名失效
    jar = {settings.oidc_cookie_name: tampered}

    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)
    assert parse_redirect(res)["q"]["sso_error"] == "state_expired", (
        "被改过的票据和过期票据对外是同一个错误码：不给攻击者区分「签名错」还是「过期」"
    )
    assert db.query(User).count() == 0


def test_callback_with_wrong_state_is_rejected(client, sso_env, db):
    jar = put_state_cookie(client, state="right")
    res = client.get(f"{SSO_CALLBACK}?code=abc&state=wrong", cookies=jar, follow_redirects=False)
    assert parse_redirect(res)["q"]["sso_error"] == "state_mismatch"
    assert db.query(User).count() == 0


def test_callback_when_user_cancels_at_keycloak(client, sso_env):
    jar = put_state_cookie(client)
    res = client.get(f"{SSO_CALLBACK}?error=access_denied&state=s1", cookies=jar, follow_redirects=False)
    assert parse_redirect(res)["q"]["sso_error"] == "access_denied"


def test_callback_when_exchange_rejected(client, sso_env, monkeypatch, db):
    def _boom(**_):
        raise AppError("SSO_TOKEN_REJECTED", "x", 401, {"error": "invalid_grant"})

    monkeypatch.setattr(oidc, "exchange_code", _boom)
    jar = put_state_cookie(client)
    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)
    assert parse_redirect(res)["q"]["sso_error"] == "sso_token_rejected"
    assert db.query(User).count() == 0


def test_callback_with_forged_signature(client, sso_env, fake_keycloak, monkeypatch, db):
    """用**另一个**私钥签出来的 id_token —— 公钥对不上，必须拒。

    刻意不 mock 密钥来源：fake_keycloak 已经把 JWKS 换成「Keycloak 的真公钥」，
    而这枚 token 是攻击者私钥签的，验签必然失败。
    """
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = make_id_token(attacker_key, nonce="n1")
    monkeypatch.setattr(oidc, "exchange_code", lambda **_: {"id_token": forged})

    jar = put_state_cookie(client)
    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)
    assert parse_redirect(res)["q"]["sso_error"] == "sso_bad_id_token"
    assert db.query(User).count() == 0


def test_callback_with_wrong_nonce(client, sso_env, fake_keycloak, monkeypatch, db):
    """令牌是真的、签名也对，但 nonce 不是这次登录发的 —— 典型的重放。"""
    token = make_id_token(fake_keycloak, nonce="someone-elses-nonce")
    monkeypatch.setattr(oidc, "exchange_code", lambda **_: {"id_token": token})

    jar = put_state_cookie(client, nonce="my-nonce")
    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)
    assert parse_redirect(res)["q"]["sso_error"] == "sso_bad_nonce"
    assert db.query(User).count() == 0


# ---------- 3. 成功：新用户自动建号 ----------


def test_callback_creates_new_user_and_returns_local_jwt(
    client, sso_env, fake_keycloak, monkeypatch, db
):
    token = make_id_token(fake_keycloak, nonce="n1", username="sso_bob")
    monkeypatch.setattr(oidc, "exchange_code", lambda **_: {"id_token": token})
    jar = put_state_cookie(client)

    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)
    assert res.status_code == 302
    info = parse_redirect(res)
    assert info["path"] == FRONTEND_PATH
    assert "token" in info["q"], info
    assert "sso_error" not in info["q"]

    user = db.query(User).filter(User.username == "sso_bob").one()
    assert user.auth_source == "keycloak"
    assert user.keycloak_sub == "kc-sub-1"
    assert user.role == "user", "SSO 新用户只能是普通用户，不能凭外部身份当管理员"
    assert user.password_hash is None, "SSO 账号本地不该有口令"
    assert user.last_login_at is not None

    # 发出去的是**本地** JWT，能被现有 decode_token 解开
    assert decode_token(info["q"]["token"]) == user.id

    # 票据一次性：回调后 Cookie 被清掉
    assert not res.cookies.get(settings.oidc_cookie_name)


def test_callback_reuses_bound_user(client, sso_env, fake_keycloak, monkeypatch, db):
    user = User(username="sso_bob", keycloak_sub="kc-sub-1", auth_source="keycloak", role="user")
    db.add(user)
    db.commit()
    db.refresh(user)
    uid = user.id

    token = make_id_token(fake_keycloak, nonce="n1", username="sso_bob")
    monkeypatch.setattr(oidc, "exchange_code", lambda **_: {"id_token": token})
    jar = put_state_cookie(client)

    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)
    assert decode_token(parse_redirect(res)["q"]["token"]) == uid
    assert db.query(User).count() == 1, "第二次登录不该再建一个号"


def test_callback_rejects_disabled_user(client, sso_env, fake_keycloak, monkeypatch, db):
    user = User(
        username="sso_bob",
        keycloak_sub="kc-sub-1",
        auth_source="keycloak",
        role="user",
        status="disabled",
    )
    db.add(user)
    db.commit()

    token = make_id_token(fake_keycloak, nonce="n1", username="sso_bob")
    monkeypatch.setattr(oidc, "exchange_code", lambda **_: {"id_token": token})
    jar = put_state_cookie(client)

    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)
    info = parse_redirect(res)
    assert info["q"]["sso_error"] == "user_disabled"
    assert "token" not in info["q"], "禁用账号不能拿到任何令牌"


def test_callback_tolerates_small_clock_skew(client, sso_env, fake_keycloak, monkeypatch):
    """iat/nbf 比本机晚几秒（虚拟机时钟漂移）时要能登进去。

    真实翻车现场：Keycloak 在 Rocky 虚拟机里，和 Windows 时钟差 1 秒以上，
    刚签发的 id_token 的 iat「来自未来」，PyJWT 2.10+ 零容差直接抛
    `The token is not yet valid (iat)`，用户看到的却是「身份凭证校验失败」。
    这条用例锁住 oidc._JWT_LEEWAY_SECONDS，谁把它删了这里就红。
    """
    put_state_cookie(client)
    now = int(time.time())
    token = make_id_token(ks_key=fake_keycloak, nonce="n1", iat=now + 5, exp=now + 305)
    monkeypatch.setattr(oidc, "exchange_code", lambda **_kw: {"id_token": token})

    res = client.get(
        SSO_CALLBACK,
        params={"code": "c1", "state": "s1"},
        cookies={
            settings.oidc_cookie_name: sso_state.issue(
                app="life-web", state="s1", code_verifier="v1", nonce="n1"
            )
        },
        follow_redirects=False,
    )

    assert res.status_code == 302, res.text
    assert "token" in parse_redirect(res)["q"]


def test_callback_drops_external_next(client, sso_env, fake_keycloak, monkeypatch):
    token = make_id_token(fake_keycloak, nonce="n1")
    monkeypatch.setattr(oidc, "exchange_code", lambda **_: {"id_token": token})
    jar = put_state_cookie(client, next_path="//evil.com/steal")

    res = client.get(f"{SSO_CALLBACK}?code=abc&state=s1", cookies=jar, follow_redirects=False)
    q = parse_redirect(res)["q"]
    assert q.get("next", "") == "", "站外路径必须被丢弃"
    assert q["token"], "丢弃 next 不影响登录本身"


# ---------- 4. sso_sync 的业务判定（脱离 HTTP 直接测） ----------


def test_sync_links_existing_local_account_by_username(db):
    local = User(username="demo", role="admin", password_hash="x" * 10, auth_source="local")
    db.add(local)
    db.commit()
    db.refresh(local)

    user = auth_service.sso_sync(db, sub="kc-1", username="demo", email=None)
    assert user.id == local.id, "同名本地账号应被认领，而不是新建一个"
    assert user.keycloak_sub == "kc-1"
    assert user.auth_source == "keycloak"
    assert user.role == "admin", "认领不改本地角色"


def test_sync_never_steals_an_already_bound_account(db):
    db.add(User(username="taken", keycloak_sub="kc-other", auth_source="keycloak", role="user"))
    db.commit()

    with pytest.raises(AppError) as exc:
        auth_service.sso_sync(db, sub="kc-1", username="taken", email=None)
    assert exc.value.status_code == 409


def test_sync_rejects_soft_deleted_user(db):
    db.add(
        User(
            username="gone",
            keycloak_sub="kc-gone",
            auth_source="keycloak",
            role="user",
            deleted_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()

    with pytest.raises(AppError) as exc:
        auth_service.sso_sync(db, sub="kc-gone", username="gone", email=None)
    assert exc.value.status_code == 401, "软删账号不能被 Keycloak 令牌复活"


def test_sync_creates_user_with_empty_email(db):
    """Keycloak 没给邮箱时不能写空串：MySQL 唯一索引允许多个 NULL，不允许多个空串。"""
    user = auth_service.sso_sync(db, sub="kc-noemail", username="noemail", email=None)
    assert user.email is None
    assert user.role == "user"