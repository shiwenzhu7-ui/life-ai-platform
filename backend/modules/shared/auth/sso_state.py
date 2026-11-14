"""SSO 中转站在浏览器里留的一次性容器（V2.0）。

「点登录」和「Kuixcloak 回调」是两个完全独立的 HTTP 请求，中间隔着用户在
Keycloak 页面上输密码的那几十秒。后端必须记住三样东西，否则回调时无从校验：

    state         防 CSRF：回调带回来的 state 必须和这里存的一样
    code_verifier  PKCE：换 token 时要用它算出 code_challenge 的原像
    nonce         防重放：Keycloak 会把它原样写进 id_token

**为什么不建表存**：
    这是「几十秒就作废」的临时数据，存库要额外一张表 + 定期清理 + 多一次查询。
    签成 JWT 塞进 HttpOnly Cookie 更省：浏览器帮你保管，我们用 JWT_SECRET 验真，
    伪造不了；HttpOnly 让页面上的 JS（包括被 XSS 注入的脚本）读不到它。

**为什么用 JWT 而不是明文 Cookie**：
    明文的话攻击者能改 state/code_verifier。签名之后改一个字节就验不过。

Cookie 属性：
    HttpOnly   JS 读不到 → XSS 偷不走 code_verifier
    SameSite=Lax  跨站 POST 不带 Cookie，挡掉大部分 CSRF；但 Keycloak 回调是
                  顶层 GET 跳转，Lax 允许带上，所以流程不受影响
    Path       只发给 SSO 那两个接口，别把票据撒到全站
    max_age    默认 600 秒，超时就是「登录超时，请重试」
    secure     HTTPS 才加；本地 http://localhost 加了会导致 Cookie 根本不下发
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import jwt

from core.config import settings
from core.errors import AppError

# 这个 JWT 和登录令牌是两回事：它只活 10 分钟、只装流程票据、带自己的类型标记。
# 加 typ 是为了防止「把 SSO 票据当登录令牌用」的混淆（反之亦然）。
_TOKEN_TYPE = "sso_state"


def _secret() -> str:
    if not settings.jwt_secret.strip():
        raise RuntimeError("找不到 JWT_SECRET，请检查 backend/.env（见 .env.example）")
    return settings.jwt_secret


def issue(*, state: str, code_verifier: str, nonce: str, next_path: str = "") -> str:
    """把三样票据签成一个短期 JWT，交给 router 写进 Cookie。"""
    now = int(time.time())
    payload = {
        "typ": _TOKEN_TYPE,
        "state": state,
        "cv": code_verifier,
        "nonce": nonce,
        "next": next_path,  # 登录后要不要回到某个具体页面
        "iat": now,
        "exp": now + settings.oidc_cookie_max_age,
    }
    return jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)


def read(raw: str | None) -> dict[str, Any]:
    """读回 Cookie 里的票据。任何异常都当成「这次登录作废」。

    不区分「过期」和「签名不对」：对用户都是「重来一次」，
    区分了反而给攻击者提供信息（和 core/security.decode_token 同一口径）。
    """
    if not raw:
        raise AppError("SSO_STATE_MISSING", "登录超时，请重新登录", 400)
    try:
        payload = jwt.decode(
            raw,
            _secret(),
            algorithms=[settings.jwt_algorithm],
            # 用 leeway 而不是完全放开 exp 校验：容忍 5 秒的机器间时钟漂移，
            # 但过期的票据照样判废 —— 这张票只该活 10 分钟。
            leeway=timedelta(seconds=5),
            options={"require": ["exp", "iat", "state", "cv", "nonce", "typ"]},
        )
    except jwt.InvalidTokenError:
        raise AppError("SSO_STATE_INVALID", "登录超时，请重新登录", 400) from None

    if payload.get("typ") != _TOKEN_TYPE:
        # 有人把登录令牌塞进这个 Cookie —— 拒绝，别当票据用
        raise AppError("SSO_STATE_INVALID", "登录超时，请重新登录", 400)
    return payload