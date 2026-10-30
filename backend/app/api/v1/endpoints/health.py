"""健康检查。挂在根路径，不带 /api/v1 前缀（见 docs/接口设计.md §2）。"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.time import now_tz
from app.db.session import get_db

router = APIRouter(tags=["健康检查"])


@router.get("/healthz", summary="进程存活检查")
def healthz() -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "server_time": now_tz().isoformat(),
    }


@router.get("/readyz", summary="依赖就绪检查（含 MySQL）")
def readyz(db: Session = Depends(get_db)) -> dict:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return {"status": "not_ready", "database": "error"}
    return {"status": "ready", "database": "ok"}
