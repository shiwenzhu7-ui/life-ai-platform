"""ORM 声明基类。

所有模型继承 Base，统一由 Alembic 自动发现（见 alembic/env.py 的 target_metadata）。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    pass
