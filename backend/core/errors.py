"""统一错误处理。

所有可预期的业务错误都抛 AppError，由全局处理器转成统一错误体
（格式见 docs/接口设计.md §1.2）：

    {"code": "NOT_FOUND", "message": "待办不存在", "request_id": null, "detail": null}

规则：堆栈只进日志，绝不返回给客户端（NFR 11.3）。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """业务异常。service 层抛它，不抛裸 HTTPException。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        detail: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        # 少数错误需要额外响应头：401 要带 WWW-Authenticate: Bearer，
        # 前端拦截器靠它区分「该跳登录」和「该提示权限不足」。
        self.headers = headers


def _body(code: str, message: str, detail: dict | None = None) -> dict:
    return {"code": code, "message": message, "request_id": None, "detail": detail}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(exc.code, exc.message, exc.detail),
        headers=exc.headers,
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """把 Pydantic 的 422 转成统一错误体。

    原始 errors() 里的 ctx 可能含 ValueError 对象、不能直接序列化，
    这里只保留 loc / msg / type 三个字段。
    """
    raw = getattr(exc, "errors", lambda: [])()
    errors = [
        {
            "loc": [str(part) for part in item.get("loc", ())],
            "msg": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        }
        for item in raw
    ]
    return JSONResponse(
        status_code=422,
        content=_body("VALIDATION_ERROR", "参数校验失败", {"errors": errors}),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 完整堆栈只进日志
    logger.exception("未捕获异常: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=_body("INTERNAL_ERROR", "服务器内部错误，请稍后重试"),
    )


# 框架自己抛的 HTTPException（路径没注册 → 404、方法不对 → 405）也要走统一错误体，
# 否则前端会看到两套形状：{"code": ...} 与 FastAPI 默认的 {"detail": ...}。
_HTTP_CODE_MAP = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
}


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = _HTTP_CODE_MAP.get(exc.status_code, "HTTP_ERROR")
    if exc.status_code == 404 and exc.detail in ("Not Found", "Not found"):
        message = "接口不存在"  # 路由未注册，不是业务意义上的"资源不存在"
    else:
        message = str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(code, message),
        headers=getattr(exc, "headers", None),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """在 app 上注册全部异常处理器。

    app.py 里只调这一行；以后加新处理器（比如 IntegrityError → 409）也改这里，
    不用动 app.py。
    """
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


# ---------- 常用错误工厂（service 层直接用这些，不手写字符串） ----------


def not_found(message: str = "资源不存在") -> AppError:
    return AppError("NOT_FOUND", message, 404)


def forbidden(message: str = "无权访问") -> AppError:
    return AppError("FORBIDDEN", message, 403)


def unauthorized(message: str = "未登录或登录已过期") -> AppError:
    # 401 必须带 WWW-Authenticate（RFC 7235）；浏览器/axios 靠它决定是否清令牌跳登录
    return AppError("UNAUTHORIZED", message, 401, headers={"WWW-Authenticate": "Bearer"})


def user_disabled(message: str = "账号已被禁用") -> AppError:
    """令牌有效但账号被停用。和 401 分开：这不是「重新登录能解决」的问题。"""
    return AppError("USER_DISABLED", message, 403)


def conflict(message: str = "资源冲突") -> AppError:
    return AppError("CONFLICT", message, 409)


def validation_error(
    message: str, detail: dict | None = None, *, code: str = "VALIDATION_ERROR"
) -> AppError:
    """422：参数在当前状态下不合法。

    管理后台的分级闸门用 `code` 细分（ROOT_PROTECTED / CANNOT_GRANT_ADMIN 等），
    前端据此决定文案，不靠 message 字符串匹配。默认值保持老行为。
    """
    return AppError(code, message, 422, detail)
