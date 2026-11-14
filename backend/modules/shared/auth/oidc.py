"""OIDC 授权码流程的「协议部分」（V2.0 SSO）。

这个文件只做三件事，全部与业务无关：

    build_authorize_url   拼 Keycloak 授权页的 URL（带 state / nonce / PKCE）
    exchange_code         拿 code 去换 access_token + id_token（secret 只出现在这里）
    decode_id_token       验证并解开 id_token，拿到「你是谁」的那几条 claim

**为什么单独一个文件**：协议细节（PKCE 怎么算、id_token 用哪个算法验、JWKS 从哪拿）
一集中，以后换身份服务器（Keycloak → Authentik / Azure AD / Authing）只改这里。
service.py 那边只认一个返回 {sub, preferred_username, email} 的字典。

三条安全约定：
    1. client_secret 永远不进 URL、不进日志、不进响应体。它在 exchange_code 的
       POST body 里出现一次，就这一次（NFR 11.1）。
    2. id_token 的签名**必须验**。Keycloak 用 RS256 签，公钥从 JWKS 端点拿，
       所以我们这边不用配任何密钥 —— 这正是不用前端直连方案的根本好处。
    3. state / nonce / code_verifier 一次性使用，回调后 cookie 立即清掉。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from core.config import settings
from core.errors import AppError

logger = logging.getLogger(__name__)

# Keycloak 的端点全部挂在 issuer 下面这一条固定前缀上，
# 所以配置里只要给 issuer，其余端点不用一个个填（.well-known 文档里也是这几个）。
_KC = "/protocol/openid-connect"

# 换 token 的超时：这是「后端 → Keycloak」的内网调用，正常 100ms 级；
# 给 10 秒是留足虚拟机卡顿（Milvus/MySQL 抢内存时 Keycloak 会慢）。
_HTTP_TIMEOUT = 10.0

# JWKS 公钥缓存：PyJWKClient 内部按 lifespan 缓存，3600 秒拉一次足够。
# Keycloak 换签名密钥是极低频操作，真换了最多 1 小时内本进程还不能验新 token。
_JWKS_CACHE_SECONDS = 3600

# 校验时间类 claim 的容差。Keycloak 与 Windows 主机之间只要差 1 秒，
# 刚签发的 id_token 的 iat 就会「来自未来」，PyJWT 2.10+ 默认零容差会直接拒掉，
# 表现为登录明明输对了密码却报 SSO_BAD_ID_TOKEN（日志 `The token is not yet valid (iat)`）。
# 60 秒是官方文档建议的上限：既吃得下时钟漂移，又不给重放留明显窗口。
_JWT_LEEWAY_SECONDS = 60

_jwk_client: PyJWKClient | None = None


def _endpoint(path: str) -> str:
    return f"{settings.oidc_issuer.rstrip('/')}{_KC}{path}"


def sso_configured(app: str = "") -> bool:
    """这个前端能不能用 SSO。router 用它决定是「发起授权」还是 503。"""
    return settings.oidc_ready_for(app)


def client_credentials(app: str) -> tuple[str, str]:
    """取某个前端的 (client_id, client_secret)，没配就 503。

    这是唯一一处「app 名 → 密钥」的转换点：router 只传 app 名，
    永远不自己拼 secret，避免哪个分支忘了换而用错客户端的凭据。
    """
    found = settings.oidc_client_of(app)
    if not found:
        raise AppError(
            "SSO_NOT_CONFIGURED",
            "单点登录未配置，请先用账号密码登录",
            503,
        )
    return found


def new_state() -> str:
    """一次性的 state。

    它的作用不是加密，而是**防 CSRF**：别人诱导你的浏览器带着他的 code 回调时，
    state 对不上 —— 因为攻击者不知道你浏览器里那个 cookie 里装的是什么。
    """
    return secrets.token_urlsafe(32)


def new_nonce() -> str:
    """id_token 里的 nonce。

    防的是「令牌重放」：把别人抢到的 id_token 直接塞给我们。
    这里签发时给一个随机串，Keycloak 会原样写进 id_token，回调时比对。
    """
    return secrets.token_urlsafe(16)


def new_code_verifier() -> str:
    """PKCE 的 verifier：43~128 位的高熵随机串（RFC 7636）。"""
    return secrets.token_urlsafe(48)


def code_challenge_of(verifier: str) -> str:
    """verifier → challenge，S256 方式：BASE64URL(SHA256(verifier)) 并去掉 '='。

    Keycloak 客户端上选的是 S256，所以这里不能退回 plain。
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(*, app: str, state: str, nonce: str, code_verifier: str) -> str:
    """拼出浏览器要跳转的授权页地址。

    scope 只要 openid 三件套：
        openid   必须有，否则 Keycloak 不返回 id_token
        profile  给 preferred_username / name
        email    给 email（本地建号时用，可空）
    多余的用户不要（roles 之类我们不用 Keycloak 的角色，角色归本地 users.role）。

    client_id 与 redirect_uri 都按 app 取：life-web 和 admin-web 在 Keycloak 里
    是两个客户端，各自的「有效的重定向URI」也不同，混用会被 Keycloak 拒。
    """
    client_id, _ = client_credentials(app)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge_of(code_verifier),
        "code_challenge_method": "S256",
    }
    return f"{_endpoint('/auth')}?{urlencode(params)}"


def exchange_code(*, app: str, code: str, code_verifier: str) -> dict[str, Any]:
    """用 code 换令牌。**后端**发起，client_secret 只出现在这里的请求体里。

    失败一律转成 401 —— 这条链路上出错（code 过期、secret 错、Keycloak 挂了）
    对用户来说都是同一件事：「这次登录没成功，再点一次」。
    但日志里要分清是哪一种，所以把上游的 error 字段带进 detail。

    **secret 按 app 取**：用 admin-web 的 code 配 life-web 的 secret 去换，
    Keycloak 会回 invalid_client —— 所以 app 必须从 state 票据里来，
    不能从请求参数里来（那等于让外人选择用哪套密钥）。
    """
    client_id, client_secret = client_credentials(app)
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }
    try:
        resp = httpx.post(_endpoint("/token"), data=payload, timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("OIDC 换 token 请求失败: %s", exc)
        raise AppError("SSO_UNAVAILABLE", "身份服务器连接失败，请稍后重试", 503) from None

    if resp.status_code != httpx.codes.OK:
        # 不把 resp.text 整个塞进响应体：里面可能回显 client_id 等细节
        try:
            upstream = resp.json()
        except ValueError:
            upstream = {}
        logger.warning(
            "OIDC 换 token 被拒: status=%s error=%s description=%s",
            resp.status_code,
            upstream.get("error"),
            upstream.get("error_description"),
        )
        raise AppError(
            "SSO_TOKEN_REJECTED",
            "登录校验未通过，请重新登录",
            401,
            {"status": resp.status_code, "error": upstream.get("error")},
        )

    data = resp.json()
    if not data.get("id_token"):
        raise AppError("SSO_NO_ID_TOKEN", "身份服务器未返回身份令牌", 502)
    return data


def _get_jwks_client() -> PyJWKClient:
    """JWKS 客户端单例。

    PyJWKClient 自己按 kid 缓存公钥，进程内只建一次即可。
    不预热：第一次调用时才拉，避免启动时连不上 Keycloak 就起不来。
    """
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(
            _endpoint("/certs"),
            cache_keys=True,
            lifespan=_JWKS_CACHE_SECONDS,
            timeout=_HTTP_TIMEOUT,
        )
    return _jwk_client


def decode_id_token(id_token: str, *, app: str, nonce: str) -> dict[str, Any]:
    """验证 id_token 的签名/签发者/受众/有效期/nonce，然后返回 claims。

    验哪些：
        signature  Keycloak 的 RS256 公钥（JWKS），验不过 = 令牌是伪造的
        iss        必须等于配置的 issuer（防止把别的 realm 的令牌骗过来）
        aud        必须包含**这个 app 的** client_id（防止拿给别的客户端的令牌）
        exp        过期时间
        nonce      必须等于我们自己发的那个（防重放）

    验不过一律 401，对外只说「登录校验未通过」。
    """
    client_id, _ = client_credentials(app)
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],  # 写死算法，防止 alg 混淆攻击
            audience=client_id,
            issuer=settings.oidc_issuer.rstrip("/"),
            leeway=_JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.InvalidTokenError as exc:
        logger.warning("id_token 校验失败: %s", exc)
        raise AppError("SSO_BAD_ID_TOKEN", "登录校验未通过，请重新登录", 401) from None
    except Exception as exc:  # noqa: BLE001 —— 拉 JWKS 的网络错误也要落到 401/503 而不是 500
        logger.warning("id_token 校验异常（可能是拉 JWKS 失败）: %s", exc)
        raise AppError("SSO_UNAVAILABLE", "身份服务器连接失败，请稍后重试", 503) from None

    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        # 用时间恒定比较，避免通过响应耗时猜 nonce
        raise AppError("SSO_BAD_NONCE", "登录校验未通过，请重新登录", 401)

    return claims


def claims_identity(claims: dict[str, Any]) -> dict[str, str | None]:
    """从 claims 里取 sso_sync 关心的三样东西。

    sub  唯一且永不变 —— 本地绑定的依据，**只能用这个**。
    preferred_username  Keycloak 里的用户名：第一次 SSO 登录时用来认领本地同名的老账号。
    email  可能为空，可能不唯一（Keycloak 默认不强制邮箱唯一），所以业务上不许拿它认人。
    """
    return {
        "sub": str(claims.get("sub") or ""),
        "username": claims.get("preferred_username") or claims.get("name"),
        "email": claims.get("email"),
    }