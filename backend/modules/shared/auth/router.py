"""认证接口（V1.2）—— docs/接口设计.md §3。

    POST /auth/register  201  注册，返回 {id, username, role}
    POST /auth/login     200  登录，返回 access_token / token_type / expires_in / user
    POST /auth/logout    204  退出（本地 JWT 阶段无状态，前端清 token）
    GET  /auth/me        200  当前用户信息

本文件只做两件事：把请求体交给 service、把返回值翻译成状态码。
业务判断一律在 service.py —— 这样 service 能脱离 HTTP 单测。
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import get_current_user
from modules.shared.auth import service
from modules.shared.auth.schema import LoginIn, RegisterIn, TokenOut, UserBrief, UserOut
from modules.shared.users.model import User

router = APIRouter(prefix="/auth", tags=["认证"])


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

    保留这个接口而不是让前端「直接不发请求」：V2.0 换 Keycloak 时要在这里
    调 SSO 统一退出，那时候前端不用改代码。
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut, summary="当前用户信息")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UserOut:
    return service.me(db, user)