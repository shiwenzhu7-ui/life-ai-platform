"""数据库连接与会话。

约定：业务代码通过 `Depends(get_db)` 获取 Session，不在模块层复用 Session。
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,  # 防止 MySQL 8 小时断连后拿到失效连接
    pool_recycle=3600,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个 Session，结束后关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
