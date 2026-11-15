"""认证接口（V1.2 / V2.0 SSO）—— docs/接口设计.md §3。

    POST /auth/register      201  注册，返回 {id, username, role}
    POST /auth/login         200  登录，返回 access_token / token_type / expires_in / user
    POST /auth/logout        204  退出（本地 JWT 阶段无状态，前端清 token）
    GET  /auth/me            200  当前用户信息（V2.0 另支持按 Keycloak 令牌当场换本地令牌）
    GET  /auth/sso/login     302  跳到 Keycloak 授权页（V2.0）
    GET  /auth/sso/callback  302  换到本地 JWT 后送回前端（V2.0）

本文件只做三件事：把请求体交给 service、写/清 Cookie、把返回值翻译成状态码或跳转。
业务判断一律在 service.py —— 这样 service 能脱离 HTTP 单测。

V2.0 的 BFF 分工（为什么 secret 只在后端、为什么前端不用改请求头）：
    浏览器 ──> /auth/sso/login ──302──> Keycloak
    浏览器 在 Keycloak 上输密码（本站全程碰不到密码）
    Keycloak ──302──> /auth/sso/callback?code=…&state=…
    后端用 code + client_secret 换 id_token，验完签名 → 落到本地 users 一行
    后端签发**自己的** JWT ──302──> 前端 /sso/callback?token=<本地JWT>
    之后的请求照旧带 Authorization: Bearer <本地JWT>，require_role 那一套原封不动。
"""

import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from core.config import settings
from core.db import get_db
from core.deps import get_current_user
from core.errors import AppError
from core.security import create_access_token
from core.time import now_utc
from modules.shared.auth import oidc, service, sso_state
from modules.shared.auth.schema import LoginIn, RegisterIn, TokenOut, UserBrief, UserOut
from modules.shared.users.model import User

router = APIRouter(prefix="/auth", tags=["认证"])

# 静默探测（prompt=none）时 Keycloak 表示「这人没登着」的 error 取值。
# 只在票据里记着 silent=True 时才翻译成 sso_silent —— 交互式登录本来就不该收到这两个。
_SILENT_ERRORS = {"login_required", "interaction_required"}


@router.post(
    "/register",
    response_model=UserBrief,
    status_code=status.HTTP_201_CREATED,
    summary="注册（注册后要再调一次 /auth/login 拿令牌）",
)
def register(data: RegisterIn, db: Session = Depends(get_db)) -> User:
    return service.register(db, data)


@router.post("/login", response_model=TokenOut, summary="登录，换取 access_token")
def login(data: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    return service.login(db, data)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="退出登录（本地 JWT 阶段仅前端清令牌）",
)
def logout(user_id=Depends(get_current_user)) -> Response:
    """令牌是无状态的，后端没有会话可销毁。

    这里的 204 仍然保留，给「本地账号密码登录」和旧前端用。
    SSO 会话里的「退出」要比本地多一步：还得清掉 Keycloak 那边的会话 Cookie，
    否则单点登录的意义就没了 —— 下次点登录 Keycloak 认出你还登录着，直接放行。
    那一跳由前端自己拼 URL 完成（同源 Cookie 只认 Keycloak 那个域名），
    端点取 settings.oidc_issuer + /protocol/openid-connect/logout。
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut, summary="当前用户信息")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UserOut:
    return service.me(db, user)


# ---------- V2.0：SSO（BFF 模式）----------


def _sso_cookie_path() -> str:
    """Cookie 只发给 SSO 那两个接口，别把票据撒到全站。"""
    return f"{settings.api_v1_prefix}/auth/sso"


def _set_state_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        key=settings.oidc_cookie_name,
        value=value,
        max_age=settings.oidc_cookie_max_age,
        path=_sso_cookie_path(),
        httponly=True,  # JS 读不到
        samesite="lax",  # Keycloak 回跳是顶层 GET，Lax 允许带上
        secure=settings.oidc_redirect_uri.startswith("https://"),  # http 下加了 Cookie 根本不下发
    )


def _clear_state_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.oidc_cookie_name,
        path=_sso_cookie_path(),
        httponly=True,
        samesite="lax",
        secure=settings.oidc_redirect_uri.startswith("https://"),
    )


@router.get(
    "/sso/login",
    summary="跳转到 Keycloak 登录（302）",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
)
def sso_login(next_path: str = "", app: str = "", silent: bool = False) -> RedirectResponse:
    """发起授权码流程。

    1. 生成 state / nonce / code_verifier（PKCE）
    2. 把票据（含发起方 app）签成短期 JWT 写进 HttpOnly Cookie
    3. 302 到 Keycloak 授权页

    `next` 是登录成功后前端要回的站内路径，夹在 Cookie 里而不是 URL 里，
    回调时再拼，避免中间人手改。

    `app` 是哪个前端在发起：life-web / admin-web。不传就当 life-web（老前端不传）。
    它决定三件事：用哪个 client_id + secret、成功后 302 回哪个前端、
    以及 state 票据里记下的「是哪个应用」。见 sso_state.issue 的说明。

    `silent=True` 走 prompt=none：**绝不弹登录框**，只问 Keycloak「这人还登着吗」。
    前端用它实现「A 应用登录过，打开 B 应用直接进去」。没登着时 Keycloak 回
    error=login_required，回调会把 sso_error 换成 sso_silent，前端据此安静地
    显示登录表单 —— 不能弹红字，否则每打开一次登录页都报一次错。
    """
    target = app or settings.oidc_default_app
    if not oidc.sso_configured(target):
        raise AppError(
            "SSO_NOT_CONFIGURED",
            "单点登录未配置，请先用账号密码登录",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    state = oidc.new_state()
    nonce = oidc.new_nonce()
    verifier = oidc.new_code_verifier()

    cookie_value = sso_state.issue(
        app=target,
        state=state,
        code_verifier=verifier,
        nonce=nonce,
        next_path=next_path,
        silent=silent,
    )
    response = RedirectResponse(
        url=oidc.build_authorize_url(
            app=target,
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            prompt="none" if silent else "",
        ),
        status_code=status.HTTP_302_FOUND,
    )
    _set_state_cookie(response, cookie_value)
    return response


@router.get(
    "/sso/callback",
    summary="Keycloak 回调：换 token 并签本地 JWT（302 回前端）",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
)
def sso_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
) -> RedirectResponse:
    """回调。任何一个环节不对都 302 回前端并带上 ?sso_error=…

    为什么不直接抛 401/400：
        这是用户在浏览器里跟着跳转的最后一跳，JSON 错误体他看不懂也回不去。
        带着错误原因回到前端登录页，用已有的 Message 组件弹一句人话，体验才对。

    顺序（每一步都不能少）：
        cookie 存在且验签通过 → state 比对 → code 非空 → 换 token
        → 验 id_token（签名/iss/aud/exp/nonce）→ 落本地用户 → 禁用检查 → 签本地 JWT

    **是哪个前端发起的，只认 Cookie 票据里签过的那一份**（saved["app"]）。
    回调 URL 上的 ?app= 不参与决策 —— 否则谁都能指定「用 admin-web 的 secret 换」。
    错误回跳也用这个 app 选前端地址，免得管理端失败把用户丢到生活端登录页。
    """
    fallback = f"{settings.oidc_frontend_callback}"

    def front_end_of(app_name: str) -> str:
        return settings.oidc_frontend_callback_of(app_name) or fallback

    def back_with_error(code_: str, app_name: str = "") -> RedirectResponse:
        response = RedirectResponse(
            url=f"{front_end_of(app_name)}?sso_error={code_}", status_code=status.HTTP_302_FOUND
        )
        _clear_state_cookie(response)  # 失败也清，别让旧票据留着
        return response

    # 回调 URL 上只认这些：其它（含 login_required / interaction_required，
    # 它们是静默探测的「没登录」信号，不能直接甩给前端当错误）一律算 sso_denied。
    try:
        saved = sso_state.read(request.cookies.get(settings.oidc_cookie_name))
    except AppError:
        return back_with_error("state_expired")

    app_name = str(saved.get("app") or "")
    redirect = front_end_of(app_name)
    saved_silent = bool(saved.get("silent"))

    if error:
        if error == "access_denied":
            return back_with_error("access_denied", app_name)
        if error in _SILENT_ERRORS and saved_silent:
            return back_with_error("sso_silent", app_name)
        return back_with_error("sso_denied", app_name)

    # 时间恒定比较：不让外部通过响应耗时猜 state
    if not state or not secrets.compare_digest(saved["state"], state):
        return back_with_error("state_mismatch", app_name)
    if not code:
        return back_with_error("missing_code", app_name)

    try:
        tokens = oidc.exchange_code(app=app_name, code=code, code_verifier=saved["cv"])
        claims = oidc.decode_id_token(tokens["id_token"], app=app_name, nonce=saved["nonce"])
        ident = oidc.claims_identity(claims)
        user = service.sso_sync(
            db, sub=ident["sub"], username=ident["username"], email=ident["email"]
        )
    except AppError as exc:
        # token_rejected / bad_id_token / bad_nonce / unconfigured
        return back_with_error(exc.code.lower(), app_name)

    if user.status != "active":
        return back_with_error("user_disabled", app_name)

    user.last_login_at = now_utc()
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id)
    next_path = saved.get("next") or ""
    # 只接受站内路径：挡住 //evil.com 与 http://…，避免开放重定向
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""

    # id_token 一并交给前端（它只存 sessionStorage）：退出时要拿它当
    # id_token_hint，后端才知道该退哪一个 Keycloak 会话，见 /sso/logout/confirm。
    # 它不是本站的通行证 —— 前端除了退出之外不会用它，API 请求一律带我们的本地 JWT。
    #
    # 令牌当一次性票据用：带着 token 和 next 跳到前端，前端读完**立刻把地址栏洗掉**
    # （见 life-web/src/views/sso/callback.vue）。这样令牌只在浏览器历史和
    # referrer 里存在一瞬，不会留在用户随时能截图的地址栏上。
    params = urlencode({"token": token, "id_token": tokens["id_token"], "next": next_path})
    response = RedirectResponse(
        url=f"{redirect}?{params}",
        status_code=status.HTTP_302_FOUND,
    )
    _clear_state_cookie(response)
    return response


@router.get(
    "/sso/logout",
    summary="退出账号中心会话（302 到登录页）",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
)
def sso_logout(
    app: str = "",
    redirect: str = "",
) -> RedirectResponse:
    """退出的「无 id_token」兜底：能去 Keycloak 就去，去不了才只退本地。

    正常退出走 /sso/logout/confirm（前端带着 id_token 来）。这个接口留给
    「前端拿不到 id_token」的场景 —— 比如用户清过 sessionStorage、或者用
    账号密码登录进来、或者升级前登录的旧会话。

    即便是这种场景，**也要去 Keycloak 走一趟**：实测只带 client_id +
    post_logout_redirect_uri、不带 id_token_hint，Keycloak 仍会清掉浏览器里的
    KEYCLOAK_SESSION。否则「另一个前端」的静默探测还能成功，用户会以为没退掉。

    为什么单独一个接口而不是让 confirm 自己降级：confirm 是 GET，前端跳到它
    就没法再执行 JS 把 id_token 补上了。分成两步之后，前端可以在「有
    id_token」和「没有」之间自己选，后端只负责跳。
    """
    front = settings.oidc_frontend_login_of(app or settings.oidc_default_app)
    fallback = _safe_next(redirect, front)
    landing = _absolute_frontend(fallback, app or settings.oidc_default_app)
    url = oidc.build_logout_url(
        app=app or settings.oidc_default_app, id_token="", post_logout_redirect=landing
    )
    return RedirectResponse(url=url or fallback, status_code=status.HTTP_302_FOUND)


@router.get(
    "/sso/logout/confirm",
    summary="带 id_token 的第二跳：清掉 Keycloak 会话（302）",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
)
def sso_logout_confirm(
    app: str = "",
    id_token: str = "",
    redirect: str = "",
) -> RedirectResponse:
    """前端把 sessionStorage 里的 id_token 交上来，这里转成「清掉 Keycloak 会话」的一跳。

    分工：
        前端      清自己的 token，再整页跳到这个接口
        这里      302 到 Keycloak 的 logout（带 id_token_hint）
        Keycloak  验 id_token、清 KEYCLOAK_SESSION，再 302 回 post_logout_redirect_uri
        前端      落回登录页；此时静默探测会收到 sso_silent，安静显示表单

    为什么这么绕（前端存 id_token，再带回来）：后端无状态，不建会话表，
    就不可能自己知道「这个人上一次登录时 Keycloak 发的 id_token 是什么」。
    唯一可靠的持有者就是发起登录的那个浏览器。

    为什么确认这一步是 GET 且**不做鉴权**：点退出时本地 JWT 往往已经过期
    （前端先清 token），但 Keycloak 会话还活着 —— 那恰恰是最需要退出的场景。
    退错人最多是「把别人的 Keycloak 会话退掉」，代价远小于「退不掉」。

    任何一步不满足都退回登录页而不是报错：退出失败的最差结果应当是「本地退了、
    Keycloak 没退」，用户可以再点一次；不该是「点了退出看到一页 500」。
    """
    front = settings.oidc_frontend_login_of(app or settings.oidc_default_app)
    fallback = _safe_next(redirect, front)
    # 退出后的落点必须是「绝对 URL 且已在该客户端的注销白名单里」。
    # 前端只传站内路径（/login），这里拼上该应用自己的 origin 变成
    # http://localhost:8082/login —— Keycloak 不认相对路径。
    landing = _absolute_frontend(fallback, app or settings.oidc_default_app)

    # 没有 id_token 也去 Keycloak 走一趟：带 client_id + post_logout_redirect_uri
    # 不带 id_token_hint，Keycloak 同样会清 KEYCLOAK_SESSION。少了这一跳，
    # 另一个前端（life-web）的静默探测还能成功，"退出"就成了摆设。
    url = oidc.build_logout_url(
        app=app or settings.oidc_default_app, id_token=id_token, post_logout_redirect=landing
    )
    # 没配 issuer、或该 app 没配 client → 退化成「只退本地」
    return RedirectResponse(url=url or fallback, status_code=status.HTTP_302_FOUND)


def _absolute_frontend(path: str, app: str) -> str:
    """把站内路径拼成该应用前端的绝对地址。

    /login  ->  http://localhost:8082/login
    用的是 OIDC_FRONTEND_LOGINS 里登记过的地址做基底（那个地址本身就已在
    Keycloak 的「有效的注销后重定向URI」白名单里）。拼不出来就原样返回，
    让 Keycloak 去拒 —— 本地不做白名单猜测。
    """
    base = settings.oidc_frontend_login_of(app)
    if not base:
        return path
    try:
        parts = urlsplit(base)
    except ValueError:
        return path
    if not parts.scheme or not parts.netloc:
        return path
    origin = f"{parts.scheme}://{parts.netloc}"
    if path.startswith(origin):
        return path
    return origin + path


def _safe_next(value: str, fallback: str) -> str:
    """只放行站内路径，其余一律回落到登录页。"""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return fallback

