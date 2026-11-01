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

logger = logging.getLogger(__name__)


class AppError(Exception):
    """业务异常。service 层抛它，不抛裸 HTTPException。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


def _body(code: str, message: str, detail: dict | None = None) -> dict:
    return {"code": code, "message": message, "request_id": None, "detail": detail}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(exc.code, exc.message, exc.detail),
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


def register_exception_handlers(app: FastAPI) -> None:
    """在 app 上注册全部异常处理器。

    app.py 里只调这一行；以后加新处理器（比如 IntegrityError → 409）也改这里，
    不用动 app.py。
    """
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


# ---------- 常用错误工厂（service 层直接用这些，不手写字符串） ----------


def not_found(message: str = "资源不存在") -> AppError:
    return AppError("NOT_FOUND", message, 404)


def forbidden(message: str = "无权访问") -> AppError:
    return AppError("FORBIDDEN", message, 403)


def unauthorized(message: str = "未登录或登录已过期") -> AppError:
    return AppError("UNAUTHORIZED", message, 401)


def conflict(message: str = "资源冲突") -> AppError:
    return AppError("CONFLICT", message, 409)


def validation_error(message: str, detail: dict | None = None) -> AppError:
    return AppError("VALIDATION_ERROR", message, 422, detail)
