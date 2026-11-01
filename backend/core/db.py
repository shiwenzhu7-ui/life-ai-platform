"""数据库连接与会话 —— 整个后端访问 MySQL 的唯一入口。

分工：
  Base          所有表模型的父类（SQLAlchemy 靠它收集「有哪些表」）
  engine        全程序共用一个（里面是连接池）
  SessionLocal  造会话的工厂
  get_db()      每个请求一个会话，请求结束自动关闭
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from core.config import settings

# 缺失配置时立刻报错，不要带病运行到一个看不懂的连接错误
if not settings.database_url:
    raise RuntimeError("找不到 DATABASE_URL，请检查 backend/.env 是否存在")


class Base(DeclarativeBase):
    """所有表模型的父类。

    注意：**全站只有这一个 Base**。不要在别的文件里再声明一个，
    否则同一个进程里会出现两套互不相识的表定义。
    """

    pass


# pool_pre_ping：取连接前先探活，防止 MySQL 主动断开后拿到死连接
# init_command：每条连接建立时把 MySQL 会话时区设成 UTC。
#   不设的话 MySQL 的 NOW() 会返回东八区时间，和 Python 写入的 UTC 混在一张表里，
#   同一条数据的 created_at 会比 due_at 多 8 小时，非常难查。
engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args={"init_command": "SET time_zone = '+00:00'"},
)

# autoflush=False：不自动刷库，提交时机自己控制
# expire_on_commit=False：commit 之后对象属性仍可用，不用重新查一遍
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个 Session，请求结束一定关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
