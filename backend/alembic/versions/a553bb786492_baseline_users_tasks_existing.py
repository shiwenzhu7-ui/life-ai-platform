"""baseline：承认 users / tasks 两张表已经存在

Revision ID: a553bb786492
Revises:
Create Date: 2026-11-01 15:35:28.487467

为什么这个迁移是空的？
    库里 `users`、`tasks` 两张表在本工程接入 Alembic 之前就已经用
    `Base.metadata.create_all()` 建好了。如果第一条迁移就写 CREATE TABLE，
    升级时会直接撞 "Table 'users' already exists"。

    所以第一步是一个**空迁移**，只用来标记「这一刻的库结构算作起点」：
        alembic stamp a553bb786492
    stamp 只写 `alembic_version` 表里的版本号，不执行任何 DDL。

    之后再写真正的新迁移，Alembic 才知道该从哪一步往后加。
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a553bb786492"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """什么都不做：这一步代表「已存在的 users / tasks」。"""


def downgrade() -> None:
    """什么都不做：撤销到「没有 Alembic 管理」的状态即可，不删表。"""
