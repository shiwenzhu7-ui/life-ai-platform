"""统一错误处理：业务异常与未捕获异常都转成统一错误体。

错误体格式见 docs/接口设计.md §1.2：
    {"code": "...", "message": "...", "request_id": "...", "detail": {...}}
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """业务异常。所有可预期的业务错误都应抛它，而不是 HTTPException。"""

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


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "request_id": getattr(request.state, "request_id", None),
            "detail": exc.detail,
        },
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """把 FastAPI/Pydantic 的 422 转成统一错误体。

    Pydantic 原始 errors() 里 ctx 可能含 ValueError 对象，不能直接 json 序列化，
    这里只保留 loc / msg / type 三个字段。
    """
    raw_errors = getattr(exc, "errors", lambda: [])()
    errors = [
        {
            "loc": [str(part) for part in item.get("loc", ())],
            "msg": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        }
        for item in raw_errors
    ]
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "参数校验失败",
            "request_id": getattr(request.state, "request_id", None),
            "detail": {"errors": errors},
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 完整堆栈只进日志，绝不返回给客户端
    logger.exception("未捕获异常: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "服务器内部错误，请稍后重试",
            "request_id": getattr(request.state, "request_id", None),
            "detail": None,
        },
    )


# 常用错误工厂
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
